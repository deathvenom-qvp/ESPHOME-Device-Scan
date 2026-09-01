#!/usr/bin/env python3
"""Dump what this Home Assistant instance actually returns.

The device/entity registry WebSocket commands are what the Home Assistant
frontend uses, but they are not in the public API documentation, so their exact
names and payload shapes are the one thing this add-on cannot confirm without a
live instance. Run this inside the add-on container to check them:

    docker exec addon_<slug> python3 /opt/edscan/scripts/probe_ha.py

It reports, for each command, whether it succeeded and how many rows came back,
then confirms the two facts generation depends on: that ESPHome devices carry a
``mac`` entry in ``connections``, and that config entry titles look like ESPHome
node names.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp

from app.ha_client import (
    CMD_CONFIG_ENTRIES,
    CMD_DEVICE_REGISTRY,
    CMD_ENTITY_REGISTRY,
    CMD_GET_STATES,
    HaApiError,
    SupervisorHaClient,
)
from app.models import ESPHOME_NAME_RE


async def probe() -> int:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        print("SUPERVISOR_TOKEN is not set; run this inside the add-on container.")
        return 1

    base = os.environ.get("EDSCAN_SUPERVISOR_URL", "http://supervisor/core")
    async with aiohttp.ClientSession() as session:
        client = SupervisorHaClient(session, base_url=base, token=token)

        print(f"Probing {base}\n")
        for command in (
            CMD_CONFIG_ENTRIES,
            CMD_DEVICE_REGISTRY,
            CMD_ENTITY_REGISTRY,
            CMD_GET_STATES,
        ):
            try:
                result = await client._ws_command(command)  # noqa: SLF001 - a probe
                count = len(result) if isinstance(result, list) else "n/a"
                print(f"  OK    {command:<34} rows={count}")
            except HaApiError as err:
                print(f"  FAIL  {command:<34} {err}")

        flows = await client.list_discovery_flows()
        print(f"  ---   discovery flows (esphome)     rows={len(flows)}\n")

        entries = await client.list_config_entries("esphome")
        devices = await client.list_devices()
        entry_ids = {e.get("entry_id") for e in entries}

        print(f"ESPHome config entries: {len(entries)}")
        for entry in entries[:10]:
            title = entry.get("title", "")
            looks_like_node = bool(ESPHOME_NAME_RE.match(str(title).strip()))
            print(
                f"  title={title!r:<34} state={entry.get('state')!r:<16}"
                f" valid-node-name={looks_like_node}"
            )

        matched = [
            d for d in devices
            if ({d.get("primary_config_entry")} | set(d.get("config_entries") or []))
            & entry_ids
        ]
        with_mac = [
            d for d in matched
            if any(
                str(c[0]).lower() == "mac"
                for c in (d.get("connections") or [])
                if isinstance(c, (list, tuple)) and len(c) == 2
            )
        ]
        print(f"\nESPHome devices in registry: {len(matched)}")
        print(f"  ...with a MAC in connections: {len(with_mac)}")
        if matched and not with_mac:
            print("  WARNING: no MACs found. ${mac} placeholders will be stripped.")

        if matched:
            print("\nSample device row:")
            print(json.dumps(matched[0], indent=2, default=str)[:1600])

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(probe()))
