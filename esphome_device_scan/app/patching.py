"""Text-level editing primitives.

The generator never re-serialises YAML. It collects :class:`TextEdit` objects
describing exact source ranges to replace, then applies them right-to-left so
earlier offsets stay valid. Everything outside an edit is byte-identical to the
template, which is what "preserve all other template content" demands.
"""

from __future__ import annotations

import logging
import re

from .models import TextEdit

_LOGGER = logging.getLogger(__name__)

#: A scalar we can leave unquoted: no YAML indicators, no leading/trailing
#: space, and not something the resolver would read as a bool/null/number.
_PLAIN_SAFE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")

_RESERVED_PLAIN = frozenset(
    {
        "true", "false", "yes", "no", "on", "off", "null", "none", "~",
        "y", "n",
    }
)


def apply_edits(source: str, edits: list[TextEdit] | tuple[TextEdit, ...]) -> str:
    """Apply non-overlapping edits to ``source``.

    Edits are sorted and applied from the end of the document backwards, so an
    earlier edit never shifts a later edit's offsets. Overlapping edits are a
    programming error; the overlapping one is dropped with a warning rather
    than producing silently corrupt YAML.
    """
    ordered = sorted(edits, key=lambda e: (e.start, e.end))

    accepted: list[TextEdit] = []
    previous_end = -1
    for edit in ordered:
        if edit.start < previous_end:
            _LOGGER.warning(
                "Dropping overlapping edit (%s) at [%d,%d)",
                edit.reason, edit.start, edit.end,
            )
            continue
        if not (0 <= edit.start <= edit.end <= len(source)):
            _LOGGER.warning(
                "Dropping out-of-range edit (%s) at [%d,%d)",
                edit.reason, edit.start, edit.end,
            )
            continue
        accepted.append(edit)
        previous_end = edit.end

    result = source
    for edit in reversed(accepted):
        result = result[: edit.start] + edit.replacement + result[edit.end :]
    return result


def quote_like(value: str, style: str | None) -> str:
    """Render ``value`` in the same quoting style the template used.

    Keeping the original style is what makes a generated file read as though a
    human edited one line of the template, rather than as machine output.
    """
    if style == '"':
        return _double_quote(value)
    if style == "'" and "\n" not in value and "\r" not in value:
        return "'" + value.replace("'", "''") + "'"
    # The template had it unquoted; keep it that way when still safe to do so,
    # otherwise fall back to double quotes rather than emit invalid YAML.
    if _is_plain_safe(value):
        return value
    return _double_quote(value)


def _is_plain_safe(value: str) -> bool:
    """Whether ``value`` can be written unquoted and read back unchanged.

    The check is a real round trip through the YAML loader rather than a
    hand-written list of dangerous shapes, because that list is impossible to
    get right: ``123456`` loads as an int, ``0x1f`` as 31, ``1_000`` as 1000,
    ``2024-01-01`` as a date, ``1e5`` as a float. Emitting any of those
    unquoted would hand ESPHome a non-string where it requires a string.
    """
    if not _PLAIN_SAFE_RE.match(value):
        return False
    if value.lower() in _RESERVED_PLAIN:
        return False

    from .yaml_compat import YamlParseError, load

    try:
        loaded = load(f"probe: {value}\n")
    except YamlParseError:
        return False
    return isinstance(loaded, dict) and loaded.get("probe") == value


#: Characters that cannot appear literally inside a double-quoted YAML scalar.
_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _double_quote(value: str) -> str:
    """Double-quote ``value``, escaping everything YAML requires.

    Control characters matter here because friendly names come from Home
    Assistant, where a user can type anything into a device's name field. A raw
    newline spliced into a scalar would silently produce a broken config.
    """
    out = []
    for char in value:
        if char in _ESCAPES:
            out.append(_ESCAPES[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\x{ord(char):02x}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def line_bounds(source: str, index: int) -> tuple[int, int]:
    """Offsets of the whole line containing ``index``, including its newline."""
    start = source.rfind("\n", 0, index) + 1
    newline = source.find("\n", index)
    end = len(source) if newline == -1 else newline + 1
    return start, end
