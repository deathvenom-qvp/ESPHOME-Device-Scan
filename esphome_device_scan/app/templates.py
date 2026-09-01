"""Base template loading and device-to-template matching.

Matching directives live in ``# x-...:`` comments in the template's header
rather than in a YAML key, so a template stays a byte-for-byte valid ESPHome
config that ``esphome config`` can validate and a human can flash directly:

    # x-match-prefix: cloudbay-t
    # x-match-regex:  ^cb-t-\\d+$
    # x-match-model:  CloudBay T
    # x-mac-policy:   suffix3
    # x-priority:     10

With no directives at all, the filename stem acts as the prefix -- so dropping
in ``cloudbay-t.yaml`` is enough to claim every ``cloudbay-t-*`` device.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from .models import Device, MacPolicy, TemplateMatch, TemplateSpec

_LOGGER = logging.getLogger(__name__)

#: `# x-key: value`, case-insensitive, tolerant of spacing.
_DIRECTIVE_RE = re.compile(r"^\s*#\s*x-([a-z-]+)\s*:\s*(.+?)\s*$", re.IGNORECASE)

#: Directives are only read from the header block: the run of comments and
#: blank lines before the first real YAML key. This keeps an `# x-...` string
#: deep inside a config from silently changing matching behaviour.
_HEADER_LIMIT = 60

TEMPLATE_SUFFIXES = (".yaml", ".yml")

# Rule precedence, most specific first. Baked into the match score so ordering
# is total and stable rather than dependent on directory iteration order.
_RULE_RANK = {"regex": 400, "prefix": 300, "filename-prefix": 200, "model": 100}


class TemplateRepository:
    """Loads templates from a directory, newest content on every scan."""

    def __init__(self, templates_dir: Path, seed_dir: Path | None = None) -> None:
        self._dir = templates_dir
        self._seed_dir = seed_dir

    @property
    def directory(self) -> Path:
        return self._dir

    def ensure_seeded(self) -> int:
        """Copy bundled examples in when the templates dir is new or empty.

        Returns the number of files copied. Never overwrites: a user's own
        template of the same name always wins.
        """
        if not self._seed_dir or not self._seed_dir.is_dir():
            return 0
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            _LOGGER.error("Cannot create templates dir %s: %s", self._dir, err)
            return 0

        if any(self._dir.glob("*.yaml")) or any(self._dir.glob("*.yml")):
            return 0

        copied = 0
        for src in sorted(self._seed_dir.iterdir()):
            if src.suffix.lower() not in TEMPLATE_SUFFIXES:
                continue
            dest = self._dir / src.name
            if dest.exists():
                continue
            try:
                shutil.copyfile(src, dest)
                copied += 1
            except OSError as err:
                _LOGGER.error("Could not seed template %s: %s", src.name, err)
        if copied:
            _LOGGER.info("Seeded %d example template(s) into %s", copied, self._dir)
        return copied

    def load_all(self) -> list[TemplateSpec]:
        """Read every template in the directory. Unreadable files are skipped."""
        if not self._dir.is_dir():
            _LOGGER.warning("Templates directory %s does not exist", self._dir)
            return []

        specs: list[TemplateSpec] = []
        for path in sorted(self._dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in TEMPLATE_SUFFIXES:
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as err:
                _LOGGER.error("Skipping template %s: %s", path.name, err)
                continue
            specs.append(parse_template(path, raw))

        if not specs:
            _LOGGER.warning("No templates found in %s", self._dir)
        return specs


def parse_template(path: Path, raw: str) -> TemplateSpec:
    """Build a TemplateSpec, reading `# x-...` directives from the header."""
    prefixes: list[str] = []
    regexes: list[str] = []
    models: list[str] = []
    priority = 0
    mac_policy: MacPolicy | None = None
    warnings: list[str] = []

    for line in raw.splitlines()[:_HEADER_LIMIT]:
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("#"):
            break  # first real YAML content ends the header block

        match = _DIRECTIVE_RE.match(line)
        if not match:
            continue
        key, value = match.group(1).lower(), match.group(2).strip()

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
    """Chooses the single best template for a device."""

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

        # Implicit rule: with no explicit prefix or regex, the filename stem is
        # the prefix. This is what makes `cloudbay-t.yaml` work with no config.
        if (
            not template.match_prefixes
            and not template.match_regexes
            and prefix_matches(device.node_name, template.stem)
        ):
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
