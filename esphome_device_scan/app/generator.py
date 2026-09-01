"""Turn a base template into a per-device ESPHome config.

Design note -- why this is not a load/dump round trip
-----------------------------------------------------
``generate()`` is a **pure function** of ``(template.raw, device, policy)``. It
performs no I/O, reads no clock and uses no randomness, which is exactly what
makes the output deterministic and idempotent: running it twice yields byte-
identical text, so a rescan has nothing to write.

It works by composing the template into a node graph purely to *locate* the
handful of values that must change (see :mod:`.yaml_compat`), collecting
:class:`TextEdit` ranges, and splicing those into the original source. Comments,
key order, quoting style, blank lines and anchors outside the edited ranges come
through untouched because they are literally never re-serialised.

What gets modified, and nothing else:

* ``esphome.name``           -- set to the discovered node name
* ``esphome.friendly_name``  -- only when it contains a ``${...}`` placeholder
* ``name_add_mac_suffix``    -- the MAC-suffix logic proper
* ``substitutions.*``        -- the name/mac substitutions a template declares
* ``${mac}`` / ``${name}``   -- inline placeholders, when not declared above
"""

from __future__ import annotations

import logging
import re

from . import yaml_compat as yc
from .models import (
    Device,
    GeneratedYaml,
    MacPolicy,
    MacSuffixAction,
    TemplateSpec,
    TextEdit,
)
from .patching import apply_edits, line_bounds, quote_like

_LOGGER = logging.getLogger(__name__)

#: Substitution keys a template might use for the device's own name. ESPHome
#: has no convention here, so we accept the ones people actually write.
NAME_SUBSTITUTION_KEYS = (
    "devicename",
    "device_name",
    "name",
    "nodename",
    "node_name",
    "hostname",
)

MAC_SUBSTITUTION_KEYS = ("mac", "mac_suffix", "macaddr", "mac_address")


def _placeholder_re(key: str) -> re.Pattern[str]:
    """Match ``${key}`` and the brace-less ``$key`` form ESPHome also accepts."""
    return re.compile(r"\$\{" + re.escape(key) + r"\}|\$" + re.escape(key) + r"\b")


def _strip_placeholder_re(key: str) -> re.Pattern[str]:
    """As above, but also eats one separator immediately before the placeholder.

    ``"${name}-${mac}"`` should strip to ``"${name}"``, not ``"${name}-"``.
    """
    return re.compile(
        r"[-_]?(?:\$\{" + re.escape(key) + r"\}|\$" + re.escape(key) + r"\b)"
    )


class YamlGenerator:
    """Renders per-device YAML from a base template."""

    def __init__(
        self,
        mac_policy: MacPolicy = MacPolicy.SUFFIX3,
        suffix_action: MacSuffixAction = MacSuffixAction.SET_FALSE,
        *,
        add_header: bool = True,
    ) -> None:
        self._mac_policy = mac_policy
        self._suffix_action = suffix_action
        self._add_header = add_header

    # -- public API ------------------------------------------------------

    def generate(self, template: TemplateSpec, device: Device) -> GeneratedYaml:
        """Render ``template`` for ``device``. Pure; safe to call repeatedly."""
        source = template.raw
        warnings: list[str] = []
        edits: list[TextEdit] = []

        # A per-template directive beats the global option.
        policy = template.mac_policy or self._mac_policy
        mac_token = self._mac_token(device, policy)
        if device.mac is None and policy is not MacPolicy.STRIP:
            warnings.append(
                "Home Assistant did not expose a MAC for this device; "
                "MAC placeholders were removed instead of substituted."
            )

        root = yc.compose(source)
        if root is None:
            raise yc.YamlParseError(f"Template '{template.name}' is empty")

        # Ranges already spoken for, so the inline placeholder pass does not
        # try to edit inside a value we have replaced wholesale.
        claimed: list[tuple[int, int]] = []

        handled_mac_key = self._patch_substitutions(
            root, source, device, mac_token, edits, claimed, warnings
        )
        self._patch_esphome_block(root, source, device, edits, claimed, warnings)
        self._patch_inline_placeholders(
            root, source, device, mac_token, handled_mac_key, edits, claimed, warnings
        )

        if self._add_header:
            edits.append(self._header_edit(source, template, device))

        content = apply_edits(source, edits)
        if not content.endswith("\n"):
            content += "\n"

        return GeneratedYaml(
            content=content,
            edits=tuple(sorted(edits, key=lambda e: e.start)),
            warnings=tuple(warnings),
        )

    # -- individual transformations --------------------------------------

    def _patch_substitutions(
        self,
        root,
        source: str,
        device: Device,
        mac_token: str | None,
        edits: list[TextEdit],
        claimed: list[tuple[int, int]],
        warnings: list[str],
    ) -> bool:
        """Point a template's own ``substitutions:`` at this device.

        Editing the substitution's *value* is preferred over inlining at every
        use site: it is a one-line diff and keeps the template's single source
        of truth intact. Returns True when a MAC substitution was handled, so
        the inline pass knows to leave ``${mac}`` references alone.
        """
        block = yc.map_get(root, "substitutions")
        if block is None:
            return False

        # Every declared name-ish key is patched, not just the first match. A
        # template declaring both `devicename` and `name` would otherwise keep
        # a stale value in the second, and because the inline pass skips any
        # key the template declares, `${name}` would then still resolve to the
        # template's placeholder.
        for key in NAME_SUBSTITUTION_KEYS:
            node = yc.map_get(block, key)
            if node is not None and yc.is_patchable_scalar(node, source):
                start, end = yc.node_range(node)
                edits.append(
                    TextEdit(
                        start, end,
                        quote_like(device.node_name, node.style),
                        f"substitutions.{key} -> device name",
                    )
                )
                claimed.append((start, end))

        handled_mac = False
        for key in MAC_SUBSTITUTION_KEYS:
            node = yc.map_get(block, key)
            if node is None or not yc.is_patchable_scalar(node, source):
                continue
            start, end = yc.node_range(node)
            if mac_token is None:
                # No MAC available (or policy is strip). Leaving a stale value
                # in place is misleading, so blank it and let the inline pass
                # remove the references.
                warnings.append(
                    f"substitutions.{key} left empty: no MAC value to substitute."
                )
                edits.append(
                    TextEdit(start, end, '""', f"substitutions.{key} -> empty")
                )
            else:
                edits.append(
                    TextEdit(
                        start, end,
                        quote_like(mac_token, node.style or '"'),
                        f"substitutions.{key} -> {mac_token}",
                    )
                )
                handled_mac = True
            claimed.append((start, end))

        return handled_mac

    def _patch_esphome_block(
        self,
        root,
        source: str,
        device: Device,
        edits: list[TextEdit],
        claimed: list[tuple[int, int]],
        warnings: list[str],
    ) -> None:
        """Set the node name and neutralise the MAC-suffix logic."""
        block = yc.map_get(root, "esphome")
        if block is None:
            warnings.append(
                "Template has no 'esphome:' block; the node name was not set."
            )
            return

        # -- name: always the literal discovered name -----------------------
        # This is the whole point of the add-on, so it is set unconditionally
        # rather than only when a placeholder is present. It covers
        # `name: cloudbay-t-${mac}`, `name: ${devicename}` and plain literals
        # alike, and leaves the file readable without resolving substitutions.
        name_node = yc.map_get(block, "name")
        if name_node is None:
            warnings.append("Template's esphome: block has no 'name:' key.")
        elif yc.is_patchable_scalar(name_node, source):
            start, end = yc.node_range(name_node)
            edits.append(
                TextEdit(
                    start, end,
                    quote_like(device.node_name, name_node.style),
                    f"esphome.name -> {device.node_name}",
                )
            )
            claimed.append((start, end))
        else:
            warnings.append(
                "esphome.name uses a tag, block scalar or alias and was left as is."
            )

        # -- friendly_name: only touched when it holds a placeholder --------
        friendly_node = yc.map_get(block, "friendly_name")
        if (
            friendly_node is not None
            and yc.is_patchable_scalar(friendly_node, source)
            and "$" in yc.scalar_text(friendly_node, source)
        ):
            start, end = yc.node_range(friendly_node)
            value = device.friendly_name or device.node_name
            edits.append(
                TextEdit(
                    start, end,
                    quote_like(value, friendly_node.style or '"'),
                    "esphome.friendly_name -> device friendly name",
                )
            )
            claimed.append((start, end))

        self._patch_mac_suffix_flag(block, source, edits, claimed)

    def _patch_mac_suffix_flag(
        self,
        block,
        source: str,
        edits: list[TextEdit],
        claimed: list[tuple[int, int]],
    ) -> None:
        """Handle ``name_add_mac_suffix: true``.

        This is ESPHome's real MAC-suffix mechanism (it appends the last three
        bytes of the MAC as ``<name>-aabbcc``). A per-device config has a fixed
        name, so the flag must go. Default is to rewrite it to ``false`` rather
        than delete it: the intent stays visible in the file and in any diff.
        """
        value_node = yc.map_get(block, "name_add_mac_suffix")
        if value_node is None:
            return

        value_start, value_end = yc.node_range(value_node)
        if value_start is None or value_end is None:
            return

        # Already false -- nothing to do, which also keeps regeneration a no-op.
        if str(getattr(value_node, "value", "")).strip().lower() in ("false", "no", "off"):
            return

        if self._suffix_action is MacSuffixAction.SET_FALSE:
            edits.append(
                TextEdit(
                    value_start, value_end, "false",
                    "esphome.name_add_mac_suffix -> false",
                )
            )
            claimed.append((value_start, value_end))
            return

        # REMOVE: drop the entire physical line.
        key_node = yc.map_key_node(block, "name_add_mac_suffix")
        anchor = yc.node_range(key_node)[0] if key_node else value_start
        line_start, line_end = line_bounds(source, anchor if anchor is not None else value_start)
        edits.append(
            TextEdit(line_start, line_end, "", "esphome.name_add_mac_suffix removed")
        )
        claimed.append((line_start, line_end))

    def _patch_inline_placeholders(
        self,
        root,
        source: str,
        device: Device,
        mac_token: str | None,
        handled_mac_key: bool,
        edits: list[TextEdit],
        claimed: list[tuple[int, int]],
        warnings: list[str],
    ) -> None:
        """Resolve ``${mac}``/``${name}`` left in ordinary scalar values.

        Operates on each scalar's **raw source slice** rather than its parsed
        value, so quoting style and any escape sequences survive verbatim -- the
        only characters that change are the placeholder's own.

        Skipped entirely for a placeholder already handled via the
        ``substitutions:`` block, since the reference resolves correctly there
        and rewriting both would be redundant noise in the diff.
        """
        declared = self._declared_substitutions(root)

        for node in yc.iter_scalars(root):
            if not yc.is_patchable_scalar(node, source):
                continue
            start, end = yc.node_range(node)
            if any(start < c_end and c_start < end for c_start, c_end in claimed):
                continue

            original = source[start:end]
            updated = original

            # -- MAC placeholders ---------------------------------------
            if not handled_mac_key:
                for key in MAC_SUBSTITUTION_KEYS:
                    if key in declared:
                        continue
                    if mac_token is None:
                        updated = _strip_placeholder_re(key).sub("", updated)
                    else:
                        updated = _placeholder_re(key).sub(mac_token, updated)

            # -- name placeholders --------------------------------------
            for key in NAME_SUBSTITUTION_KEYS:
                if key in declared:
                    continue
                updated = _placeholder_re(key).sub(device.node_name, updated)

            if updated == original:
                continue

            # Refuse an edit that would leave an empty scalar behind -- e.g.
            # `ssid: ${mac}` under the strip policy would produce `ssid:`,
            # silently turning the value into null.
            if not updated.strip(" \t\"'"):
                warnings.append(
                    f"Left {original!r} unchanged: substituting would empty the value."
                )
                continue

            edits.append(TextEdit(start, end, updated, f"placeholder {original!r}"))
            claimed.append((start, end))

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _declared_substitutions(root) -> set[str]:
        """Keys physically declared in the template's ``substitutions:`` block."""
        block = yc.map_get(root, "substitutions")
        from ruamel.yaml.nodes import MappingNode, ScalarNode

        if not isinstance(block, MappingNode):
            return set()
        return {
            key.value
            for key, _ in block.value
            if isinstance(key, ScalarNode) and isinstance(key.value, str)
        }

    def _mac_token(self, device: Device, policy: MacPolicy) -> str | None:
        """The text that should replace ``${mac}``, or None to strip it."""
        if policy is MacPolicy.STRIP or not device.mac:
            return None
        if policy is MacPolicy.FULL:
            return device.mac
        return device.mac_suffix

    def _header_edit(
        self, source: str, template: TemplateSpec, device: Device
    ) -> TextEdit:
        """Provenance comment prepended to the generated file.

        Carries no timestamp on purpose. A clock reading here would make every
        regeneration produce different bytes and break the idempotency the rest
        of this module works to guarantee.
        """
        lines = [
            "# Generated by ESPHome Device Scan from a base template.",
            f"# Template: {template.name}",
            f"# Device:   {device.node_name}",
        ]
        if device.mac_pretty:
            lines.append(f"# MAC:      {device.mac_pretty}")
        lines.append("# Safe to edit: a scan never overwrites this file.")
        header = "\n".join(lines) + "\n\n"

        # A leading document marker has to stay first.
        insert_at = 4 if source.startswith("---\n") else 0
        if insert_at == 0 and source.startswith("---\r\n"):
            insert_at = 5
        return TextEdit(insert_at, insert_at, header, "provenance header")
