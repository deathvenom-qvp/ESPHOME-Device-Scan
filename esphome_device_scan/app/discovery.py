"""Turn Home Assistant's registries into a list of ESPHome devices.

Joins four Home Assistant surfaces:

* **config entries** (``domain == "esphome"``) -- one per adopted device; the
  entry ``title`` defaults to the ESPHome node name, and ``state`` says whether
  the integration loaded.
* **device registry** -- carries the MAC in ``connections`` and the *friendly*
  name in ``name``.
* **entity registry + states** -- an ESPHome device's entities go
  ``unavailable`` when it drops off, which is our online/offline signal.
* **discovery flows** -- devices HA has seen but nobody has adopted yet.

The subtle part is the node name. The device registry's ``name`` field is
``friendly_name or name``, so on any device with a friendly name set it is the
*display* name, not the node name -- using it as a filename would be wrong. The
node name really lives in the config entry's ``data[CONF_DEVICE_NAME]``, which
the WebSocket API does not expose, so :meth:`_resolve_node_name` works down a
documented chain of fallbacks instead.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from .ha_client import HaApi
from .models import (
    ESPHOME_NAME_RE,
    Device,
    DeviceSource,
    DeviceStatus,
    normalise_mac,
    slugify_node_name,
)

_LOGGER = logging.getLogger(__name__)

#: Config entry states that mean the integration is up.
LOADED_STATES = frozenset({"loaded"})

#: Entity states that prove nothing about whether the device is reachable.
#: Note that "unknown" is *not* here: Home Assistant uses "unavailable" when an
#: integration cannot reach a device and "unknown" when it is connected but has
#: no value yet, so treating "unknown" as dead would report a freshly booted
#: device -- whose entities have not reported once -- as offline.
UNAVAILABLE_STATES = frozenset({"unavailable", ""})


class DeviceDiscoveryService:
    """Reads ESPHome devices from Home Assistant."""

    def __init__(self, ha: HaApi) -> None:
        self._ha = ha

    async def list_devices(self) -> list[Device]:
        """All known ESPHome devices, sorted by node name.

        Any single Home Assistant call may come back empty (the client logs and
        degrades rather than raising); discovery still returns whatever it could
        assemble, so a partial outage degrades the scan instead of failing it.
        """
        entries = await self._ha.list_config_entries("esphome")
        registry_devices = await self._ha.list_devices()
        entities = await self._ha.list_entities()
        states = await self._ha.list_states()
        flows = await self._ha.list_discovery_flows()

        entry_ids = {e.get("entry_id") for e in entries if e.get("entry_id")}
        state_by_entity = {
            s.get("entity_id"): s.get("state")
            for s in states
            if isinstance(s, dict) and s.get("entity_id")
        }

        # device_id -> its entity_ids, so availability can be judged per device.
        entities_by_device: dict[str, list[str]] = {}
        for entity in entities:
            device_id = entity.get("device_id")
            entity_id = entity.get("entity_id")
            if device_id and entity_id:
                entities_by_device.setdefault(device_id, []).append(entity_id)

        devices: list[Device] = []
        seen_macs: set[str] = set()
        seen_names: set[str] = set()

        for registry_device in registry_devices:
            entry = self._owning_entry(registry_device, entries, entry_ids)
            if entry is None:
                continue  # not an ESPHome device

            device = self._build_device(
                registry_device, entry, entities_by_device, state_by_entity
            )
            device = self._disambiguate(device, seen_names)
            devices.append(device)
            seen_names.add(device.node_name)
            if device.mac:
                seen_macs.add(device.mac)

        # Adopted entries whose device-registry row we could not find (rare,
        # but happens while an integration is retrying setup).
        for entry in entries:
            title = entry.get("title") or ""
            node_name = self._resolve_node_name(entry_title=title)[0]
            if not node_name or node_name in seen_names:
                continue
            devices.append(
                Device(
                    node_name=node_name,
                    friendly_name=title or None,
                    status=self._entry_status(entry),
                    source=DeviceSource.CONFIG_ENTRY,
                    entry_id=entry.get("entry_id"),
                    name_source="config-entry-title",
                )
            )
            seen_names.add(node_name)

        devices.extend(
            self._devices_from_flows(flows, seen_macs, seen_names)
        )
        return sorted(devices, key=lambda d: d.node_name)

    # -- assembly --------------------------------------------------------

    @staticmethod
    def _owning_entry(
        registry_device: dict[str, Any],
        entries: list[dict[str, Any]],
        entry_ids: set[Any],
    ) -> dict[str, Any] | None:
        """The ESPHome config entry that owns this registry device, if any."""
        primary = registry_device.get("primary_config_entry")
        candidates = set(registry_device.get("config_entries") or [])
        if primary:
            candidates.add(primary)
        matching = candidates & entry_ids
        if not matching:
            return None
        wanted = primary if primary in matching else next(iter(sorted(matching, key=str)))
        return next((e for e in entries if e.get("entry_id") == wanted), None)

    def _build_device(
        self,
        registry_device: dict[str, Any],
        entry: dict[str, Any],
        entities_by_device: dict[str, list[str]],
        state_by_entity: dict[str, Any],
    ) -> Device:
        mac = self._mac_from_connections(registry_device.get("connections"))
        registry_name = registry_device.get("name_by_user") or registry_device.get("name")

        node_name, name_source = self._resolve_node_name(
            entry_title=entry.get("title"),
            registry_name=registry_name,
            mac=mac,
        )

        device_id = registry_device.get("id")
        entity_ids = entities_by_device.get(device_id, []) if device_id else []
        status = self._status(entry, entity_ids, state_by_entity)

        return Device(
            node_name=node_name,
            friendly_name=registry_name or None,
            mac=mac,
            status=status,
            source=DeviceSource.CONFIG_ENTRY,
            device_id=device_id,
            entry_id=entry.get("entry_id"),
            model=registry_device.get("model"),
            manufacturer=registry_device.get("manufacturer"),
            sw_version=registry_device.get("sw_version"),
            area_id=registry_device.get("area_id"),
            name_source=name_source,
        )

    def _devices_from_flows(
        self,
        flows: list[dict[str, Any]],
        seen_macs: set[str],
        seen_names: set[str],
    ) -> list[Device]:
        """Devices Home Assistant has discovered but not adopted."""
        results: list[Device] = []
        for flow in flows:
            context = flow.get("context") or {}
            mac = normalise_mac(context.get("unique_id"))
            if mac and mac in seen_macs:
                continue

            placeholders = context.get("title_placeholders") or {}
            raw_name = placeholders.get("name") or flow.get("title")
            node_name, name_source = self._resolve_node_name(
                registry_name=raw_name, mac=mac
            )
            if not node_name or node_name in seen_names:
                continue

            results.append(
                Device(
                    node_name=node_name,
                    friendly_name=raw_name or None,
                    mac=mac,
                    status=DeviceStatus.DISCOVERED,
                    source=DeviceSource.DISCOVERY_FLOW,
                    name_source=name_source,
                )
            )
            seen_names.add(node_name)
            if mac:
                seen_macs.add(mac)
        return results

    @staticmethod
    def _disambiguate(device: Device, taken: set[str]) -> Device:
        """Give a device a unique node name if something already claimed it.

        Two devices can slugify to the same node name -- two "Porch Light"s in
        different rooms, say. Left alone that would have them share a filename:
        the first would get a config and the second would silently be reported
        as "already configured", pointing at a file describing another device.
        Appending the MAC suffix keeps them distinct and stable across scans.
        """
        if device.node_name not in taken:
            return device

        if device.mac:
            candidate = f"{device.node_name}-{device.mac[-6:]}"
        else:
            candidate = f"{device.node_name}-2"
        # Still colliding (two devices, same name, same MAC suffix) -- count up.
        suffix = 2
        unique = candidate
        while unique in taken:
            unique = f"{candidate}-{suffix}"
            suffix += 1

        _LOGGER.warning(
            "Two devices resolved to the node name '%s'; using '%s' for the "
            "second. Set a distinct friendly name in Home Assistant to control "
            "this.",
            device.node_name,
            unique,
        )
        return replace(device, node_name=unique, name_source=f"{device.name_source}+unique")

    # -- field derivation ------------------------------------------------

    @staticmethod
    def _mac_from_connections(connections: Any) -> str | None:
        """Pull the MAC out of a device registry ``connections`` list.

        Serialised as ``[["mac", "aa:bb:cc:dd:ee:ff"], ...]``. ESPHome always
        registers one, which is what makes registry-only discovery viable.
        """
        if not isinstance(connections, (list, tuple)):
            return None
        for connection in connections:
            if not isinstance(connection, (list, tuple)) or len(connection) != 2:
                continue
            if str(connection[0]).lower() == "mac":
                mac = normalise_mac(str(connection[1]))
                if mac:
                    return mac
        return None

    @staticmethod
    def _resolve_node_name(
        entry_title: str | None = None,
        registry_name: str | None = None,
        mac: str | None = None,
    ) -> tuple[str, str]:
        """Best guess at the ESPHome node name, with its provenance.

        Ordered by how much we trust each source:

        1. the config entry title -- ESPHome sets it from
           ``data[CONF_DEVICE_NAME]``, so when it already looks like a valid
           node name it *is* one;
        2. the registry name, slugified -- a friendly name like
           "CloudBay T Living Room" becomes ``cloudbay-t-living-room``;
        3. ``esphome-<mac suffix>`` -- last resort, always unique.
        """
        if entry_title and ESPHOME_NAME_RE.match(entry_title.strip()):
            return entry_title.strip(), "config-entry-title"

        slug = slugify_node_name(entry_title)
        if slug:
            return slug, "config-entry-title-slug"

        slug = slugify_node_name(registry_name)
        if slug:
            return slug, "registry-name-slug"

        if mac:
            return f"esphome-{mac[-6:]}", "mac-fallback"
        return "", "unresolved"

    @staticmethod
    def _entry_status(entry: dict[str, Any]) -> DeviceStatus:
        state = str(entry.get("state") or "").lower()
        return DeviceStatus.ONLINE if state in LOADED_STATES else DeviceStatus.OFFLINE

    def _status(
        self,
        entry: dict[str, Any],
        entity_ids: list[str],
        state_by_entity: dict[str, Any],
    ) -> DeviceStatus:
        """Online only when the entry loaded *and* an entity is reporting.

        A loaded config entry alone is not proof of life: ESPHome keeps the
        entry loaded and marks entities unavailable while a device is off the
        network, so the entity states are what actually distinguish the two.
        """
        if self._entry_status(entry) is not DeviceStatus.ONLINE:
            return DeviceStatus.OFFLINE
        if not entity_ids:
            return DeviceStatus.UNKNOWN

        for entity_id in entity_ids:
            state = state_by_entity.get(entity_id)
            if state is None:
                continue
            if str(state).lower() not in UNAVAILABLE_STATES:
                return DeviceStatus.ONLINE

        # Every entity we could see is unavailable. If none of them had a state
        # at all, we simply do not know.
        known = [e for e in entity_ids if e in state_by_entity]
        return DeviceStatus.OFFLINE if known else DeviceStatus.UNKNOWN
