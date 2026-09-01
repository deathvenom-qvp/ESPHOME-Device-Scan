"""ESPHome-tolerant YAML access.

Two things make ESPHome YAML awkward for generic tooling:

1. It uses custom tags -- ``!secret``, ``!include``, ``!lambda``, ``!extend``,
   ``!remove`` -- which a strict loader rejects.
2. We must edit specific values while leaving every other byte untouched, so a
   load/dump round trip is off the table.

Both are solved by ``ruamel.yaml``'s *composer*, which returns the raw node
graph. Every ``ScalarNode`` carries ``start_mark.index`` / ``end_mark.index``:
exact character offsets into the source, quotes included. That turns "edit this
value" into a precise slice replacement rather than a reformatting pass.
"""

from __future__ import annotations

import io
import logging
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

_LOGGER = logging.getLogger(__name__)

#: Tags ESPHome uses that carry opaque payloads we must never rewrite.
OPAQUE_TAGS = frozenset(
    {"!secret", "!include", "!lambda", "!extend", "!remove", "!force"}
)

#: Block scalar styles. Their source spans several lines and re-quoting them
#: safely is not worth the risk, so they are treated as unpatchable.
BLOCK_STYLES = frozenset({"|", ">"})


class YamlParseError(Exception):
    """Raised when a template or config file cannot be parsed at all."""


def _make_yaml() -> YAML:
    """A round-trip YAML instance that tolerates ESPHome's custom tags.

    The round-trip loader already represents unknown tags as ``TaggedScalar``
    rather than raising, so no constructor registration is needed; we only turn
    off the duplicate-key check that some hand-written packages trip over.
    """
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.allow_duplicate_keys = True
    return yaml


def compose(source: str) -> Node | None:
    """Compose ``source`` into a node graph, or None for an empty document.

    Raises YamlParseError when the document is genuinely malformed.
    """
    try:
        return _make_yaml().compose(io.StringIO(source))
    except YAMLError as err:
        raise YamlParseError(str(err)) from err


def load(source: str) -> Any:
    """Load ``source`` into plain Python data (tags become TaggedScalar)."""
    try:
        return _make_yaml().load(io.StringIO(source))
    except YAMLError as err:
        raise YamlParseError(str(err)) from err


def map_get(node: Node | None, key: str) -> Node | None:
    """Return the value node for ``key`` in a mapping node, else None.

    Deliberately does not follow YAML merge keys (``<<``): we patch what is
    physically written in this file, not what it inherits.
    """
    if not isinstance(node, MappingNode):
        return None
    for key_node, value_node in node.value:
        if isinstance(key_node, ScalarNode) and key_node.value == key:
            return value_node
    return None


def map_get_path(node: Node | None, *keys: str) -> Node | None:
    """Walk a chain of mapping keys, e.g. ``map_get_path(root, "wifi", "ap")``."""
    current = node
    for key in keys:
        current = map_get(current, key)
        if current is None:
            return None
    return current


def map_key_node(node: Node | None, key: str) -> ScalarNode | None:
    """Return the *key* node itself, needed when deleting a whole entry."""
    if not isinstance(node, MappingNode):
        return None
    for key_node, _ in node.value:
        if isinstance(key_node, ScalarNode) and key_node.value == key:
            return key_node
    return None


def iter_scalars(node: Node | None):
    """Yield every ScalarNode in the graph, depth first.

    Anchored nodes are visited once: the composer collapses aliases onto the
    anchor, so ``id()`` deduplication keeps us from reporting the same source
    range repeatedly.
    """
    seen: set[int] = set()

    def _walk(current: Node | None):
        if current is None or id(current) in seen:
            return
        seen.add(id(current))
        if isinstance(current, ScalarNode):
            yield current
        elif isinstance(current, MappingNode):
            for key_node, value_node in current.value:
                yield from _walk(key_node)
                yield from _walk(value_node)
        elif isinstance(current, SequenceNode):
            for item in current.value:
                yield from _walk(item)

    yield from _walk(node)


def is_patchable_scalar(node: Node | None, source: str) -> bool:
    """Whether a scalar's source range can be safely replaced.

    Rejects, in order:

    * anything that is not a plain scalar node;
    * custom-tagged values (``!secret``/``!lambda``/...), whose payload is not
      ours to rewrite;
    * block scalars (``|``, ``>``), which span lines and re-indent awkwardly;
    * ranges whose source text begins with ``&`` or ``*``. That last check is
      the important one: ruamel resolves an alias to its *anchor's* node, so an
      aliased value reports the anchor's offsets. Patching those offsets would
      silently corrupt an unrelated part of the file.
    """
    if not isinstance(node, ScalarNode):
        return False
    # Standard resolved tags look like "tag:yaml.org,2002:str"; anything
    # starting with "!" is an ESPHome custom tag whose payload is not ours.
    if str(node.tag).startswith("!"):
        return False
    if node.style in BLOCK_STYLES:
        return False

    start, end = node_range(node)
    if start is None or end is None or not (0 <= start < end <= len(source)):
        return False
    return not source[start:end].lstrip().startswith(("&", "*"))


def node_range(node: Node) -> tuple[int | None, int | None]:
    """Character offsets ``[start, end)`` of a node's source text."""
    start = getattr(node.start_mark, "index", None)
    end = getattr(node.end_mark, "index", None)
    return start, end


def scalar_text(node: Node, source: str) -> str:
    """The exact source text of a node, quotes and all."""
    start, end = node_range(node)
    if start is None or end is None:
        return ""
    return source[start:end]
