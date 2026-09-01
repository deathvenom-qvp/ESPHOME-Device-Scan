"""Finding parent templates, and matching devices to them.

Parents are discovered in the ESPHome config directory itself -- the base
firmware files you already flashed the batch from. Nothing is shipped with the
add-on and nothing is copied into your config. See :mod:`app.parents` for how a
parent is told apart from the per-device files sharing that directory.

A parent claims devices in this order, most specific first:

    # x-match-regex:  ^cb-t-\\d+$
    # x-match-prefix: cloudbay-t, cb-t
    # x-match-model:  CloudBay T

With no directives at all it still works, because a base config already says
which family it belongs to: ``name: cloudbay-t-${mac}`` implies the prefix
``cloudbay-t``. The filename stem is the last fallback.

Other directives:

    # x-template:   true | false   -- force or forbid parent classification
    # x-mac-policy: suffix3 | full | strip
    # x-priority:   10
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .models import Device, MacPolicy, TemplateMatch, TemplateSpec
from .parents import classify

_LOGGER = logging.getLogger(__name__)

TEMPLATE_SUFFIXES = (".yaml", ".yml")

#: Never candidates, whatever they contain.
IGNORED_NAMES = frozenset({"secrets.yaml", "secrets.yml"})

# Rule precedence, most specific first. Baked into the match score so ordering
# is total and stable rather than dependent on directory iteration order.
_RULE_RANK = {
    "regex": 400,
    "prefix": 300,
    "name-prefix": 250,
    "filename-prefix": 200,
    "model": 100,
}


class TemplateRepository:
    """Finds parent templates in the ESPHome config directory."""

    def __init__(self, esphome_dir: Path) -> None:
        self._dir = esphome_dir

    @property
    def directory(self) -> Path:
        return self._dir

    def load_all(self) -> list[TemplateSpec]:
        """Every parent in the directory. Unreadable files are skipped.

        Re-read on each scan, so adding a parent to the ESPHome dashboard takes
        effect without restarting the add-on.
        """
        if not self._dir.is_dir():
            _LOGGER.warning(
                "ESPHome config directory %s does not exist; no parent templates "
                "can be found.",
                self._dir,
            )
            return []

        specs: list[TemplateSpec] = []
        scanned = 0
        for path in sorted(self._dir.iterdir()):
            if not self._is_candidate(path):
                continue
            scanned += 1
            try:
                raw = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as err:
                _LOGGER.error("Skipping %s: %s", path.name, err)
                continue

            verdict = classify(raw, path)
            if not verdict.is_parent:
                continue
            _LOGGER.debug(
                "Using %s as a parent template (%s)", path.name, verdict.reason
            )
            specs.append(parse_template(path, raw, verdict.name_prefix))

        if not specs:
            _LOGGER.warning(
                "No parent templates found among %d config(s) in %s. A parent is "
                "a base config with MAC-suffix logic -- 'name: <family>-${mac}' or "
                "'name_add_mac_suffix: true' -- or any file marked "
                "'# x-template: true'.",
                scanned,
                self._dir,
            )
        return specs

    @staticmethod
    def _is_candidate(path: Path) -> bool:
        return (
            path.is_file()
            and path.suffix.lower() in TEMPLATE_SUFFIXES
            and path.name not in IGNORED_NAMES
            and not path.name.startswith(".")
        )


def parse_template(
    path: Path, raw: str, name_prefix: str | None = None
) -> TemplateSpec:
    """Build a TemplateSpec, reading `# x-...` directives from the header."""
    verdict = classify(raw, path)
    directives = verdict.directives
    if name_prefix is None:
        name_prefix = verdict.name_prefix

    prefixes: list[str] = []
    regexes: list[str] = []
    models: list[str] = []
    priority = 0
    mac_policy: MacPolicy | None = None
    warnings: list[str] = []

    for key, value in directives.items():
        if key in ("match-prefix", "match-prefixes"):
            prefixes.extend(_split_list(value))
        elif key in ("match-regex", "match-regexes"):
            for pattern in _split_list(value, separator="|||"):
                try:
                    re.compile(pattern)
                except re.error as err:
                    warnings.append(f"Invalid x-match-regex {pattern!r}: {err}")
                    continue
                regexes.append(pattern)
        elif key in ("match-model", "match-models"):
            models.extend(_split_list(value))
        elif key == "priority":
            try:
                priority = int(value)
            except ValueError:
                warnings.append(f"Invalid x-priority {value!r}; expected an integer")
        elif key == "mac-policy":
            try:
                mac_policy = MacPolicy(value.lower())
            except ValueError:
                warnings.append(
                    f"Invalid x-mac-policy {value!r}; expected suffix3, full or strip"
                )

    return TemplateSpec(
        name=path.name,
        path=path,
        raw=raw,
        match_prefixes=tuple(prefixes),
        match_regexes=tuple(regexes),
        match_models=tuple(models),
        priority=priority,
        mac_policy=mac_policy,
        warnings=tuple(warnings),
        name_prefix=name_prefix,
        detected_by=verdict.reason,
    )


def _split_list(value: str, separator: str = ",") -> list[str]:
    return [part.strip() for part in value.split(separator) if part.strip()]


def prefix_matches(node_name: str, prefix: str) -> bool:
    """Prefix match on a hyphen boundary.

    The boundary is what makes this useful rather than dangerous: prefix
    ``cloudbay-t`` must claim ``cloudbay-t-livingroom`` and the MAC-suffixed
    ``cloudbay-t-a1b2c3``, while leaving ``cloudbay-tx-porch`` -- a different
    product -- to its own template.
    """
    prefix = prefix.strip().lower()
    if not prefix:
        return False
    name = node_name.lower()
    return name == prefix or name.startswith(prefix + "-")


class TemplateMatcher:
    """Chooses the single best parent template for a device."""

    def __init__(self, templates: list[TemplateSpec] | None = None) -> None:
        self._templates: list[TemplateSpec] = list(templates or [])

    def set_templates(self, templates: list[TemplateSpec]) -> None:
        self._templates = list(templates)

    @property
    def templates(self) -> list[TemplateSpec]:
        return list(self._templates)

    def match(self, device: Device) -> TemplateMatch | None:
        """Best match for ``device``, or None.

        Every candidate is scored and the maximum taken, so the result never
        depends on which template happened to be read first.
        """
        candidates = [
            candidate
            for template in self._templates
            if (candidate := self._score(template, device)) is not None
        ]
        if not candidates:
            return None

        # Highest score wins; template name is the final, deterministic
        # tie-break so two equally-specific templates always resolve the same
        # way rather than flip-flopping between scans.
        return max(candidates, key=lambda m: (m.score, _inverse_name(m.template.name)))

    def _score(self, template: TemplateSpec, device: Device) -> TemplateMatch | None:
        """Best-scoring rule within one template, or None if it does not match."""
        best: TemplateMatch | None = None

        def consider(rule: str, pattern: str, specificity: int) -> None:
            nonlocal best
            score = (_RULE_RANK[rule] + template.priority, specificity, 0)
            if best is None or score > best.score:
                best = TemplateMatch(template, rule, pattern, score)

        for pattern in template.match_regexes:
            try:
                if re.search(pattern, device.node_name):
                    consider("regex", pattern, len(pattern))
            except re.error:
                continue  # already reported as a template warning at parse time

        for prefix in template.match_prefixes:
            if prefix_matches(device.node_name, prefix):
                consider("prefix", prefix, len(prefix))

        # Implicit rules, used when the parent declares no explicit pattern.
        # The name-derived prefix comes first: `name: cloudbay-t-${mac}` states
        # the family far more reliably than whatever the file happens to be
        # called, which may just be `base.yaml`.
        if not template.match_prefixes and not template.match_regexes:
            if template.name_prefix and prefix_matches(
                device.node_name, template.name_prefix
            ):
                consider("name-prefix", template.name_prefix, len(template.name_prefix))
            if prefix_matches(device.node_name, template.stem):
                consider("filename-prefix", template.stem, len(template.stem))

        for model in template.match_models:
            haystack = " ".join(
                part for part in (device.model, device.manufacturer) if part
            ).lower()
            if model.lower() in haystack:
                consider("model", model, len(model))

        return best


def _inverse_name(name: str) -> tuple[int, ...]:
    """Sort key making the alphabetically-first name win under ``max()``."""
    return tuple(-ord(ch) for ch in name)
