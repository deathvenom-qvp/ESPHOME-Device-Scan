# Changelog

## 1.1.0

**Parent templates now come from your ESPHome directory.** The add-on no longer
ships templates, has no `templates_dir` option, and copies nothing into your
config on first start. The base config it generates from is the one already in
`/homeassistant/esphome/` — the file you flashed the batch with.

- A file is recognised as a parent by its MAC-suffix logic: `${mac}` in
  `esphome.name`, or `name_add_mac_suffix: true`. No directives needed.
  `# x-template: true` forces it for a base without that logic;
  `# x-template: false` forces the opposite.
- A parent's family is read from its own declared name, so
  `name: cloudbay-t-${mac}` claims `cloudbay-t-*` regardless of what the file is
  called. Matching still requires a hyphen boundary.
- Parents are excluded from the "already configured" index, so a base declaring
  `name: switchboard` no longer makes a real `switchboard` device look done.
- Parents are protected from writes on every path, including Regenerate. A
  device named exactly `cloudbay-t` can no longer overwrite `cloudbay-t.yaml`.
- Generated files carry a header that marks them as children, so a scan never
  mistakes its own output for a base config.
- `map:` drops `addon_config`; the add-on needs no config directory of its own.
- The panel's Templates section now shows where parents were read from and why
  each was recognised.
- Example parents moved to `examples/parents/` as documentation and test
  fixtures only. They are never installed.

**Migrating from 1.0.0:** remove the `templates_dir` option if you set it, and
make sure your base configs are in the ESPHome directory. Anything you had in a
separate templates folder should be moved there.

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
