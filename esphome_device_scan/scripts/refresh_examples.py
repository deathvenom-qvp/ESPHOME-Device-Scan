#!/usr/bin/env python3
"""Regenerate the committed example outputs in ``examples/generated/``.

Those files are golden-file fixtures for the test suite. Run this after an
intentional change to generator behaviour, then review the resulting diff --
that diff *is* the record of what the change did to real output.

    python3 scripts/refresh_examples.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.generator import YamlGenerator  # noqa: E402
from app.models import (  # noqa: E402
    Device,
    DeviceStatus,
    MacPolicy,
    MacSuffixAction,
)
from app.templates import parse_template  # noqa: E402

OUT_DIR = ROOT / "examples" / "generated"

#: (template filename, device) pairs to render.
CASES = [
    (
        "cloudbay-t.yaml",
        Device(
            node_name="cloudbay-t-livingroom",
            friendly_name="CloudBay T Living Room",
            mac="aabbccddeeff",
            status=DeviceStatus.ONLINE,
            model="esp32dev",
            manufacturer="espressif",
        ),
    ),
    (
        "switchboard.yaml",
        Device(
            node_name="switchboard-hallway",
            friendly_name="Hallway Switchboard",
            mac="112233445566",
            status=DeviceStatus.ONLINE,
            model="esp01_1m",
            manufacturer="espressif",
        ),
    ),
]


def main() -> int:
    generator = YamlGenerator(MacPolicy.SUFFIX3, MacSuffixAction.SET_FALSE)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for template_name, device in CASES:
        path = ROOT / "examples" / "parents" / template_name
        template = parse_template(path, path.read_text(encoding="utf-8"))
        generated = generator.generate(template, device)

        destination = OUT_DIR / f"{device.node_name}.yaml"
        destination.write_text(generated.content, encoding="utf-8")
        print(f"wrote {destination.relative_to(ROOT)}")
        for warning in generated.warnings:
            print(f"  warning: {warning}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
