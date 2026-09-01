# Changelog

## 1.0.0

Initial release.

- Discovers ESPHome devices from Home Assistant's device registry, entity
  registry, config entries and in-progress discovery flows. MACs are read from
  the device registry's `connections` list, which the ESPHome integration
  populates with `CONNECTION_NETWORK_MAC`.
- Detects existing configs from both the ESPHome YAML directory (parsing each
  file's declared `esphome.name`, with substitutions resolved) and ESPHome's
  `.esphome/storage/*.json` sidecar index.
- Matches devices to base templates by filename prefix, explicit
  `# x-match-prefix`, `# x-match-regex` or `# x-match-model`, with a
  deterministic precedence order and tie-break.
- Generates per-device YAML: sets `esphome.name` to the discovered node name,
  turns off `name_add_mac_suffix`, and resolves `${mac}` / `${devicename}`
  placeholders. Everything else is preserved byte for byte, including comments,
  key order, quoting style, `!secret`/`!lambda` tags and anchors.
- Generation is deterministic and idempotent; the provenance header carries no
  timestamp so rescans produce identical files.
- A scan never overwrites an existing config. Regenerate is explicit and takes a
  timestamped backup first.
- Ingress panel: device list with node name, MAC, status, config state and
  matched template, plus Preview / Generate / View / Regenerate and a live log.
- `scripts/probe_ha.py` verifies the Home Assistant WebSocket payloads against a
  live instance.
