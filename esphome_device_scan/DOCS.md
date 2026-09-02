# ESPHome Device Scan

Finds ESPHome devices that Home Assistant knows about but that have no YAML
config, matches each to the **parent (base) config already in your ESPHome
directory**, and writes a per-device config with the real node name and the
MAC-suffix logic resolved.

Nothing is shipped with this add-on and nothing is copied into your config. The
parent it builds from is the same file you flashed the batch with.

## Installation

1. **Settings → Add-ons → Add-on Store**, then ⋮ → **Repositories**.
2. Add `https://github.com/deathvenom-qvp/ESPHOME-Device-Scan`.
3. Install **ESPHome Device Scan**, then **Start**.
4. Open **Show in sidebar** or the add-on's **Open Web UI**.

## What counts as a parent

Parents and generated children share `/homeassistant/esphome/`, so the add-on
classifies every file it finds. In order:

| # | Rule | Result |
|---|---|---|
| 1 | Carries this add-on's generated header | **child** — we wrote it |
| 2 | `# x-template: false` | **child** — forced |
| 3 | `# x-template: true`, or any `# x-match-*` directive | **parent** — forced |
| 4 | `esphome.name` contains `${mac}` / `$mac` | **parent** |
| 5 | `name_add_mac_suffix: true` | **parent** |
| 6 | anything else | **child** |

Rules 4 and 5 are why this needs no setup: MAC-suffix logic is exactly what the
add-on exists to resolve, and a per-device config never has it, because
generation strips it. Your `cloudbay-t.yaml` is already a parent as it stands.

Use `# x-template: true` for a base config that has no MAC-suffix logic, and
`# x-template: false` for a device config that legitimately keeps some.

Parents are treated as parents everywhere it matters:

- **They never count as a device's config.** A parent declaring
  `name: switchboard` will not make a real `switchboard` device look
  already-configured.
- **They are never written over**, not even by Regenerate. A device named
  exactly `cloudbay-t` would otherwise target `cloudbay-t.yaml` and destroy the
  base every child is built from.

## Which devices a parent claims

With no directives, a parent claims its own family, taken from its declared
name: `name: cloudbay-t-${mac}` claims `cloudbay-t-*`. This is read from the
name rather than the filename, so a parent called `base.yaml` still works.

Matching is on a **hyphen boundary**, so `cloudbay-t` claims
`cloudbay-t-livingroom` and `cloudbay-t-a1b2c3` but never `cloudbay-tx-porch`.

For anything more specific, add directives to the parent's header comment block
(the run of comments before the first YAML key):

```yaml
# x-match-prefix: cloudbay-t, cb-t     # comma-separated
# x-match-regex:  ^cb-t-\d+$           # full Python regex
# x-match-model:  CloudBay T           # substring of model/manufacturer
# x-mac-policy:   suffix3              # suffix3 | full | strip
# x-priority:     10                   # higher wins a tie
```

Precedence: **regex → explicit prefix → name-derived prefix → filename →
model**, then `priority`, then the longer pattern, then the alphabetically
first filename. The result is fully deterministic.

## What generation changes

Only these, and nothing else:

| Parent | Generated child |
|---|---|
| `name: cloudbay-t-${mac}` | `name: cloudbay-t-livingroom` |
| `name_add_mac_suffix: true` | `name_add_mac_suffix: false` |
| `substitutions: {devicename: …}` | set to the node name |
| `substitutions: {mac: …}` | set per `mac_policy` |
| `friendly_name: "${friendly} ${mac}"` | the device's friendly name |
| `${mac}` elsewhere, undeclared | substituted inline |

Everything else — comments, blank lines, key order, quoting style, `!secret`
and `!lambda` tags, anchors — is copied through byte for byte. The generator
locates values with a YAML parser but edits the original text, so nothing is
reformatted on the way through.

A short provenance header is prepended, naming the parent, device and MAC. It
carries no timestamp, deliberately: generation is idempotent, so regenerating an
unchanged device produces an identical file. That header is also how the add-on
knows never to mistake its own output for a parent.

## Safety

| | |
|---|---|
| A scan | Only ever **creates**. Never overwrites anything. |
| Regenerate | Per-device. Takes a `.bak-<timestamp>` copy first, and rewrites the file that actually holds the node name — so your filename is kept and you never end up with two configs claiming one name. |
| Regenerate all | The same, for every matched device at once. See below. |
| Parents | Never written to, under any path. |

### Regenerate all

The **Regenerate all** button in the panel header rebuilds every matched
device's config from its parent — the thing to press after editing a parent
and wanting the whole family brought back in line.

Before anything is written it shows exactly what will happen, split three
ways: configs it generated and nobody has touched, configs with **no** file
yet (which get created), and configs **written or edited by hand**. That last
group is the one that loses content, so it is named device by device, and the
dialog offers **Skip hand-edited** to rebuild everything else and leave those
alone.

Every replaced file is backed up as `<name>.yaml.bak-<timestamp>` first, and
parents are excluded as always. Devices with no matching parent are skipped.

### Regenerating and flashing a selection

Each parent in the **Parent templates** panel has a checkbox, with two actions
underneath:

- **Regenerate selected** — rebuilds the configs of every device claimed by the
  ticked parents, and nothing else. Backups as usual.
- **Regenerate & flash selected** — the same, then hands each config to the
  **ESPHome Device Builder** add-on to compile and upload over the air. A
  progress dialog shows every device, its state, and the live build log of the
  one currently running.

Devices are flashed **one at a time**: Device Builder compiles in a single
shared workspace, and parallel builds would contend for it. Each build can take
several minutes, and each device reboots when its firmware lands.

**Stop after this device** halts the run without interrupting the upload in
flight — aborting an OTA write part-way is how a board gets bricked.

#### Finding the Device Builder add-on

Detection tries several things, best first, so an unusual install works without
configuring anything:

1. **Supervisor discovery** — when the ESPHome add-on publishes its host and
   port under the `esphome` service. Same source Home Assistant's own ESPHome
   integration uses, and works whatever repository the add-on came from.
2. **The add-on's own info**, via `/addons/<slug>/info`, for the slug named by
   the discovery service map or one of the known ones.
3. **The host**, at the Docker gateway `172.30.32.1` and the host's own
   interface addresses.
4. **Container hostname probes**, for a dashboard that is on the bridge
   network.

Step 3 is the one that usually matters. The ESPHome add-on ships with
`host_network: true`, so it is **not** on Home Assistant's Docker bridge and
its container name (`5c53de3b-esphome`) does not route. It binds port 6052 in
the host's own network namespace, and the gateway is how a sibling add-on
reaches that. Detection reads `host_network` from the add-on's info and goes
straight to the host when it is set.

**Check builder** in the Parent templates panel re-runs all of it and reports
what it found, or every address it tried. Use it after installing or moving the
ESPHome add-on.

If it still cannot be found — a dashboard running outside Home Assistant, say —
set `esphome_dashboard_url`. A bare host, `host:port`, or a full URL all work;
the port defaults to 6052.

## MAC addresses

MACs come from Home Assistant's device registry, where the ESPHome integration
records each device with `connections={("mac", …)}`. If Home Assistant has no
MAC for a device, `${mac}` placeholders are **removed** rather than left in
place (a literal `${mac}` would fail to compile), and the panel shows a warning.

`mac_policy` controls what replaces `${mac}` outside the node name:

- `suffix3` (default) — last three bytes, `aabbcc`, matching ESPHome's own
  `name_add_mac_suffix` format
- `full` — `aabbccddeeff`
- `strip` — remove the placeholder and any separator right before it

## Options

| Option | Default | Notes |
|---|---|---|
| `esphome_config_dir` | `/homeassistant/esphome` | Where configs live **and** where parents are read from |
| `esphome_dashboard_url` | *(empty)* | Only if the ESPHome Device Builder add-on is not found automatically |
| `scan_interval_minutes` | `15` | 1–1440 |
| `auto_generate` | `true` | Off = report only |
| `scan_on_startup` | `true` | |
| `mac_policy` | `suffix3` | `suffix3` / `full` / `strip` |
| `name_add_mac_suffix_action` | `set_false` | `set_false` / `remove` |
| `dry_run` | `false` | Report without writing |
| `log_level` | `info` | |

## Troubleshooting

**"No parent templates found."**
Nothing in the ESPHome directory looked like a base config. Check that your
parent still has its MAC-suffix logic — if you have already removed it, add
`# x-template: true` to its header instead. The panel's **Parent templates**
panel shows every parent found and why it was recognised.

**A device shows "No template".**
A parent exists, but none claims that device's name. Compare the device's node
name with the parent's family prefix; remember the hyphen boundary. Add
`# x-match-prefix:` to the parent to claim more.

**"… is a parent template, not a device config."**
A device's node name collides with a parent's filename. The add-on refused to
overwrite the parent. Rename the device in Home Assistant, or mark the file
`# x-template: false` if it is not really a base config.

**"No ESPHome devices found."**
The add-on reads Home Assistant's registries, so a device must already be added
to the ESPHome integration to appear. Devices Home Assistant has *discovered*
but not adopted show up with status `discovered`.

**Devices appear but MAC is `—`.**
Home Assistant has no MAC for that device. Generation still works; `${mac}`
placeholders are stripped. Run the probe to confirm what your instance returns:

```
docker exec addon_<slug> python3 /opt/edscan/scripts/probe_ha.py
```

**The node name looks wrong.**
The ESPHome node name is not directly exposed over Home Assistant's WebSocket
API, so it is derived: config entry title first (which ESPHome sets from the
device name), then a slugified friendly name, then `esphome-<mac>`. The
`name_source` field on each row records which was used. If it guessed wrong,
create the YAML by hand — the add-on will then skip that device.

**"Could not find the ESPHome Device Builder add-on."**
Flashing needs that add-on installed and running. If it is, but under a slug
this add-on does not know, set `esphome_dashboard_url` to its internal address —
`http://<slug with underscores as hyphens>:6052`, e.g.
`http://5c53de3b-esphome:6052`.

**A flash fails immediately.**
Open the failing device in the progress dialog; the build log tail is shown
there. The usual causes are a config that does not compile, or a device that is
offline and cannot be reached over the air.

**Nothing is being written.**
Check `dry_run` and `auto_generate`, and that `esphome_config_dir` points at a
directory the add-on can write to.
