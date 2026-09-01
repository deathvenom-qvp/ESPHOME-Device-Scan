"""Reads and writes the ESPHome add-on's config directory.

Answers the question "does this device already have a YAML?" from two sources,
matching the spec's "JSON-based database or YAML directory depending on version":

1. **The YAML files themselves** -- every ``*.yaml`` is parsed and its
   ``esphome.name`` resolved through the file's own ``substitutions:``. This is
   authoritative: a file called ``lounge.yaml`` may well declare
   ``name: cloudbay-t-livingroom``, and only the declared name matters.
2. **``.esphome/storage/<file>.json``** -- the sidecar index ESPHome Device
   Builder maintains (keys ``name``, ``friendly_name``, ``address``,
   ``esp_platform``). Cheap, and covers a config whose YAML we could not parse.

Parent templates live in this same directory, so they are classified out (see
:mod:`app.parents`) on both paths:

* they are **excluded from the index**, because a parent is not any device's
  config -- a base declaring ``name: switchboard`` must not make a real
  ``switchboard`` device look already-configured; and
* they are **protected from writes**, unconditionally. Without that, a device
  named exactly ``cloudbay-t`` would target ``cloudbay-t.yaml`` -- the parent
  itself -- and Regenerate would overwrite the base config every child is
  built from.

Writes are atomic and refuse to clobber by default; only an explicit regenerate
passes ``overwrite=True``, and that takes a timestamped backup first.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import yaml_compat as yc
from .parents import classify, is_generated

_LOGGER = logging.getLogger(__name__)

#: Files in the ESPHome dir that are never device configs.
IGNORED_NAMES = frozenset({"secrets.yaml", "secrets.yml"})

CONFIG_SUFFIXES = (".yaml", ".yml")

_SUBSTITUTION_RE = re.compile(r"\$\{([a-zA-Z0-9_]+)\}|\$([a-zA-Z0-9_]+)")


class ConfigExistsError(Exception):
    """Raised when writing would overwrite a config and overwrite is False."""


class ParentTemplateError(Exception):
    """Raised when a write would land on a parent template."""


@dataclass(frozen=True)
class ExistingConfig:
    """A device config already present on disk."""

    node_name: str
    path: Path
    #: "yaml" when read from the file, "storage" when only the sidecar knew.
    source: str
    #: True when the file still carries this add-on's generated header, i.e.
    #: nobody has rewritten it since. False means hand-written, or generated and
    #: then edited -- either way, content a bulk regenerate would destroy, so
    #: the panel counts these separately before asking for confirmation.
    generated: bool = False


class EsphomeConfigStore:
    """Filesystem gateway for ``<HA config>/esphome``."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._cache: dict[str, ExistingConfig] | None = None
        self._parents: set[Path] | None = None

    @property
    def root(self) -> Path:
        return self._root

    @property
    def storage_dir(self) -> Path:
        return self._root / ".esphome" / "storage"

    def invalidate(self) -> None:
        """Drop the cached index; the next lookup re-reads the directory."""
        self._cache = None
        self._parents = None

    def parent_paths(self) -> set[Path]:
        """Files in the directory classified as parent templates."""
        if self._parents is None:
            self.index()  # populates both, from one pass over the directory
        return set(self._parents or ())

    def is_parent(self, path: Path) -> bool:
        return path in self.parent_paths()

    # -- reads -----------------------------------------------------------

    def index(self, *, refresh: bool = False) -> dict[str, ExistingConfig]:
        """Map of node name -> existing config. Cached within a scan."""
        if self._cache is not None and not refresh:
            return self._cache

        found: dict[str, ExistingConfig] = {}
        parents: set[Path] = set()

        # Sidecar index first so the authoritative YAML pass can overwrite it.
        for node_name, path in self._scan_storage_json().items():
            found[node_name] = ExistingConfig(node_name, path, "storage")
        found.update(self._scan_yaml_files(parents))

        # A parent is nobody's device config, so it must not appear in the
        # index -- including via a stale sidecar written before it became one.
        for node_name in [k for k, v in found.items() if v.path in parents]:
            del found[node_name]

        self._cache = found
        self._parents = parents
        return found

    def has_config(self, node_name: str) -> bool:
        return node_name.lower() in {k.lower() for k in self.index()}

    def find(self, node_name: str) -> ExistingConfig | None:
        index = self.index()
        if node_name in index:
            return index[node_name]
        lowered = node_name.lower()
        for key, value in index.items():
            if key.lower() == lowered:
                return value
        return None

    def read(self, node_name: str) -> str | None:
        """Source of an existing config, or None if absent/unreadable."""
        existing = self.find(node_name)
        if existing is None:
            return None
        try:
            return existing.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as err:
            _LOGGER.error("Cannot read %s: %s", existing.path, err)
            return None

    def target_path(self, node_name: str) -> Path:
        """Where a config for ``node_name`` would be written."""
        return self._root / f"{node_name}.yaml"

    def _scan_yaml_files(
        self, parents: set[Path] | None = None
    ) -> dict[str, ExistingConfig]:
        """Node name -> config, by parsing each YAML's declared esphome.name.

        One pass does triple duty: files classified as parent templates are
        collected into ``parents`` rather than indexed, and each remaining file
        is marked with whether it still carries our generated header -- all
        without reading the directory more than once.
        """
        results: dict[str, ExistingConfig] = {}
        if not self._root.is_dir():
            return results

        for path in sorted(self._root.iterdir()):
            if not path.is_file():
                continue
            if path.suffix.lower() not in CONFIG_SUFFIXES:
                continue
            if path.name in IGNORED_NAMES or path.name.startswith("."):
                continue

            source = self._read(path)
            if source is not None and classify(source, path).is_parent:
                if parents is not None:
                    parents.add(path)
                continue

            generated = source is not None and is_generated(source)
            node_name = self._declared_name(path, source)
            if node_name:
                results[node_name] = ExistingConfig(node_name, path, "yaml", generated)
            else:
                # Unparseable, or a package/fragment with no esphome block.
                # Fall back to the filename so we still never clobber it.
                results.setdefault(
                    path.stem, ExistingConfig(path.stem, path, "yaml", generated)
                )
        return results

    @staticmethod
    def _read(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as err:
            _LOGGER.debug("Cannot read %s: %s", path, err)
            return None

    def _declared_name(self, path: Path, source: str | None = None) -> str | None:
        """``esphome.name`` from a config, with substitutions resolved."""
        if source is None:
            source = self._read(path)
        if source is None:
            return None

        try:
            root = yc.compose(source)
        except yc.YamlParseError as err:
            _LOGGER.debug("Cannot parse %s: %s", path.name, err)
            return None
        if root is None:
            return None

        name_node = yc.map_get_path(root, "esphome", "name")
        raw_name = getattr(name_node, "value", None)
        if not isinstance(raw_name, str) or not raw_name:
            return None

        return self._resolve_substitutions(raw_name, root)

    @staticmethod
    def _resolve_substitutions(value: str, root) -> str:
        """Expand ``${key}``/``$key`` using the file's substitutions block.

        Single pass, no recursion: enough for real ESPHome configs and immune
        to a self-referential substitution looping forever.
        """
        from ruamel.yaml.nodes import MappingNode, ScalarNode

        block = yc.map_get(root, "substitutions")
        mapping: dict[str, str] = {}
        if isinstance(block, MappingNode):
            for key_node, value_node in block.value:
                if (
                    isinstance(key_node, ScalarNode)
                    and isinstance(value_node, ScalarNode)
                    and isinstance(value_node.value, str)
                ):
                    mapping[str(key_node.value)] = value_node.value

        def replace(match: re.Match[str]) -> str:
            key = match.group(1) or match.group(2)
            return mapping.get(key, match.group(0))

        return _SUBSTITUTION_RE.sub(replace, value)

    def _scan_storage_json(self) -> dict[str, Path]:
        """Node name -> YAML path, from ESPHome's ``.esphome/storage`` sidecars.

        Sidecar files are named after the config file (``lounge.yaml.json``),
        so the YAML path is recoverable by stripping the ``.json`` suffix.
        """
        results: dict[str, Path] = {}
        storage = self.storage_dir
        if not storage.is_dir():
            return results

        for path in sorted(storage.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as err:
                _LOGGER.debug("Skipping storage file %s: %s", path.name, err)
                continue
            if not isinstance(data, dict):
                continue
            node_name = data.get("name")
            if not isinstance(node_name, str) or not node_name:
                continue

            config_name = path.name[: -len(".json")]
            if not config_name.endswith(CONFIG_SUFFIXES):
                config_name = f"{config_name}.yaml"
            results[node_name] = self._root / config_name
        return results

    # -- writes ----------------------------------------------------------

    def write(
        self,
        node_name: str,
        content: str,
        *,
        overwrite: bool = False,
    ) -> Path:
        """Write ``<node_name>.yaml`` atomically.

        Raises ConfigExistsError unless ``overwrite`` is set. When overwriting,
        the current file is copied to ``<name>.yaml.bak-<UTC timestamp>`` first,
        so a regenerate is always recoverable.

        A regenerate rewrites the file **already holding this node name**, even
        when that file is named something else. Writing to ``<node_name>.yaml``
        instead would leave the original in place and produce two configs
        declaring the same ``esphome.name`` -- which ESPHome would then show as
        two devices fighting over one name.
        """
        existing = self.find(node_name)

        if existing is not None and not overwrite:
            raise ConfigExistsError(
                f"'{node_name}' already has a config at {existing.path}"
            )

        destination = (
            existing.path
            if (overwrite and existing is not None and existing.path.exists())
            else self.target_path(node_name)
        )

        # Unconditional, and checked even under overwrite. A device named
        # exactly `cloudbay-t` targets `cloudbay-t.yaml` -- which is the parent
        # every cloudbay-t-* config is generated from. Overwriting it would
        # destroy the base firmware and leave nothing to regenerate from.
        if self.is_parent(destination):
            raise ParentTemplateError(
                f"{destination.name} is a parent template, not a device config. "
                f"Rename the device, or mark the file '# x-template: false' if it "
                f"is not really a base config."
            )

        if destination.exists() and not overwrite:
            raise ConfigExistsError(f"{destination} already exists")

        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            raise OSError(
                f"Cannot create ESPHome config directory {self._root}: {err}"
            ) from err

        if overwrite and destination.exists():
            self._backup(destination)

        self._atomic_write(destination, content)

        # Update the cached index in place rather than dropping it. A full
        # invalidate would re-read and re-parse every config in the directory
        # after each write, making a first scan of N new devices O(N^2) file
        # parses; this keeps it linear while staying just as accurate.
        if self._cache is not None:
            self._cache[node_name] = ExistingConfig(
                node_name, destination, "yaml", is_generated(content)
            )

        _LOGGER.info("Wrote %s", destination)
        return destination

    def _backup(self, path: Path) -> Path | None:
        """Copy ``path`` aside before it is replaced.

        The name must never collide: the timestamp only has second resolution,
        so regenerating the same device twice inside one second would otherwise
        overwrite the first backup -- and by then that first backup is the only
        remaining copy of the user's original file.
        """
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        counter = 2
        while backup.exists():
            backup = path.with_name(f"{path.name}.bak-{stamp}-{counter}")
            counter += 1

        try:
            backup.write_bytes(path.read_bytes())
        except OSError as err:
            _LOGGER.error("Could not back up %s: %s", path, err)
            return None
        _LOGGER.info("Backed up %s -> %s", path.name, backup.name)
        return backup

    @staticmethod
    def _atomic_write(destination: Path, content: str) -> None:
        """Write via a temp file in the same directory, then rename.

        A half-written YAML in the ESPHome directory would be picked up by the
        dashboard and fail to compile, so the file must appear complete or not
        at all.
        """
        handle, tmp_name = tempfile.mkstemp(
            dir=str(destination.parent), prefix=f".{destination.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_path, destination)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
