# Changelog

## 1.4.1

- Fix `Could not list discovery flows: HTTP 405` on every scan. Devices Home
  Assistant has discovered but not adopted were read with
  `GET /api/config/config_entries/flow`, which Home Assistant answers with 405
  — that URL exists only to *start* a flow with POST. The list comes from the
  WebSocket command `config_entries/flow/progress` instead, which Home
  Assistant already filters down to flows it did not start itself.

  Only unadopted devices were affected; adopted ones were never read this way.

- The client no longer makes REST calls at all, so its bearer-header handling
  is gone with them.

## 1.4.0

**Flashing is removed.** Driving the ESPHome Device Builder add-on's build and
OTA pipeline from here proved unreliable across real installs, and the value it
added over opening Device Builder directly did not justify the moving parts. The
add-on goes back to doing one job well: keeping per-device YAML in step with
its parent.

Removed: the **Regenerate & flash selected** button, the flash progress dialog,
the **Check builder** button, the `esphome_dashboard_url` option, and the
`/api/flash-*` and `/api/esphome-dashboard` endpoints.

Kept: parent checkboxes, **Select all**, and **Regenerate selected**. Once a
config is written, build and install it in ESPHome Device Builder as usual.

The now-unused `esphome_dashboard_url` option is cleared from saved settings
automatically on first start, so there is nothing to edit.

## 1.3.2

Fixes flashing on a normal Home Assistant install.

- **The ESPHome add-on runs with `host_network: true`**, so it is not on Home
  Assistant's Docker bridge and its container hostname does not route. 1.3.1
  probed `5c53de3b-esphome:6052` and got nothing even when the add-on was
  running fine. Detection now reads `host_network` from the add-on's info and
  targets the host instead — the Docker gateway (`172.30.32.1`) plus the host's
  own interface addresses, read from `/network/info`.
- **A run whose builder cannot be found now fails immediately**, once, instead
  of repeating the whole search for every device. A 16-device run previously
  spent minutes rediscovering nothing and buried the useful error in sixteen
  copies of itself.
- Add-ons that are in the store but **not installed** report state `unknown`;
  these are no longer logged as "installed but unknown". A genuinely stopped
  add-on is still reported as such.
- Non-host-network dashboards now also fall back to the add-on's `ip_address`
  when its hostname does not resolve.

## 1.3.1

More robust detection of the ESPHome Device Builder add-on.

- **Supervisor discovery is now tried first.** The ESPHome add-on declares
  `discovery: [esphome]` and publishes its own host and port; this is what Home
  Assistant's own ESPHome integration reads, it is authoritative whatever
  repository the add-on came from, and `/discovery` needs no Supervisor role.
- The discovery **service map** supplies the add-on's slug even when no record
  is active, so a custom repository is handled without guessing.
- Probing tries several endpoints rather than one, so retiring a legacy route
  cannot break detection, and treats any non-5xx answer as found — a 401 still
  proves the dashboard is there.
- A **cached address that stops working is discarded**, so an add-on that
  restarts on a new container IP is re-found instead of failing forever. A
  failed search is never cached either: an ESPHome add-on installed later is
  picked up without restarting this one.
- `esphome_dashboard_url` now accepts a bare host, `host:port` or a full URL.
- Whole-sweep time is bounded, so a slow network cannot hang the panel.
- New **Check builder** button reports where the dashboard was found, or lists
  every address tried. It also appears automatically when a flash fails to
  start, which is nearly always this.
- The status endpoint never errors, even with no dashboard client configured.

## 1.3.0

- Each parent in the **Parent templates** panel now has a checkbox, plus a
  **Select all**, and two actions: **Regenerate selected** and
  **Regenerate & flash selected**.
- Flashing hands each regenerated config to the **ESPHome Device Builder**
  add-on over its WebSocket API (`/upload`, spawn protocol, `port: "OTA"`) to
  compile and upload over the air. A progress dialog shows every device, its
  state, and the live build log of the one running.
- Devices are flashed one at a time — Device Builder compiles in one shared
  workspace. **Stop after this device** ends the run without interrupting the
  upload in flight.
- The Device Builder add-on is found automatically via
  `/addons/<slug>/info` (allowed at `hassio_role: default`, so no extra
  privileges) with a direct hostname probe as fallback. The new
  `esphome_dashboard_url` option overrides it.
- New endpoints: `POST /api/regenerate-selected`, `POST /api/flash-selected`,
  `GET /api/flash/status`, `POST /api/flash/cancel`.
- Fix: regenerating a selection replaced the cached scan report, so devices
  outside the selection disappeared from the device table until the next scan.
  Results are now merged into it.

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
