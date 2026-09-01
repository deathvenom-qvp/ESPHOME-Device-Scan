# Changelog

## 1.2.0

- **Regenerate all** button in the panel header: rebuilds every matched
  device's config from its parent template in one action, for after a parent
  changes.
- It previews first. The confirmation names how many configs it generated,
  how many are missing and will be created, and — separately, device by
  device — how many were written or edited by hand and would lose that
  content. **Skip hand-edited** rebuilds everything except those.
- Every replaced file is backed up as `.bak-<timestamp>`; parents are
  excluded, as on every other path.
- New endpoints: `GET /api/regenerate-all/plan` (writes nothing) and
  `POST /api/regenerate-all[?skip_edited=1]`.
- Scan summaries now name only what happened, so a bulk run reads
  "3 regenerated" rather than "0 generated, 0 pending, 0 already configured".

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

**Migrating from 1.0.0.** The update is automatic — the add-on clears the
now-unused `templates_dir` option from your saved settings on first start, so
there is nothing to edit. Two things to do by hand:

1. Move your base configs into `/homeassistant/esphome/` (i.e. alongside your
   device configs) if they are not already there. 1.0.0 seeded its examples into
   `/addon_configs/<slug>_esphome_device_scan/templates/`; that folder is no
   longer mapped, and anything of your own in it should be moved across.
2. Check the panel's **Parent templates** section after starting. It lists every
   parent found and why. If your base config is not listed, it has no MAC-suffix
   logic — add `# x-template: true` to its header.

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
