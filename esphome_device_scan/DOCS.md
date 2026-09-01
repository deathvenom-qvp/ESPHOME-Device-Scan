# ESPHome Device Scan

Finds ESPHome devices that Home Assistant knows about but that have no YAML
config, matches each to a base template by naming pattern, and writes a
per-device config with the real node name and the MAC-suffix logic resolved.

## Installation

1. **Settings → Add-ons → Add-on Store**, then ⋮ → **Repositories**.
2. Add `https://github.com/deathvenom-qvp/ESPHOME-Device-Scan`.
3. Install **ESPHome Device Scan**, then **Start**.
4. Open **Show in sidebar** or the add-on's **Open Web UI**.

On first start the add-on copies its two example templates into
`/addon_configs/<slug>_esphome_device_scan/templates/`.

## How it decides what to do

For each ESPHome device Home Assistant knows about:

| Situation | What happens |
|---|---|
| A YAML already declares this node name | Skipped. Never touched. |
| No YAML, and a template matches | `<node-name>.yaml` is generated. |
| No YAML, no template matches | Reported in the panel so you can add a template. |
| `auto_generate: false` | Reported only; generate from the panel. |
| `dry_run: true` | Reported as "would generate"; nothing is written. |

**Existing files are never overwritten by a scan.** The only path that
overwrites is the panel's **Regenerate** button, which copies the current file
to `<name>.yaml.bak-<timestamp>` first.

## Templates

Templates live in the add-on's own config directory (`templates_dir`, default
`/config/templates` inside the container). A template is an ordinary ESPHome
config -- `esphome config` validates it, and you can flash it directly.

### Matching

With no directives, the **filename stem is the prefix**. So `cloudbay-t.yaml`
claims `cloudbay-t-livingroom`, `cloudbay-t-a1b2c3` and `cloudbay-t` itself, but
**not** `cloudbay-tx-porch` -- prefixes only match on a hyphen boundary, so a
neighbouring product name is never swallowed by accident.

For anything more specific, add directives as comments in the template header
(the run of comments before the first YAML key):

```yaml
# x-match-prefix: cloudbay-t, cb-t     # comma-separated
# x-match-regex:  ^cb-t-\d+$           # full Python regex
# x-match-model:  CloudBay T           # substring of model/manufacturer
# x-mac-policy:   suffix3              # suffix3 | full | strip
# x-priority:     10                   # higher wins a tie
```

Precedence is **regex → explicit prefix → filename prefix → model**, then
`priority`, then the longer pattern, then the alphabetically first filename.
Matching is fully deterministic: the same devices and templates always produce
the same result.

### What generation changes

Only these, and nothing else:

| Template | Generated |
|---|---|
| `name: cloudbay-t-${mac}` | `name: cloudbay-t-livingroom` |
| `name_add_mac_suffix: true` | `name_add_mac_suffix: false` |
| `substitutions: {devicename: …}` | set to the node name |
| `substitutions: {mac: …}` | set per `mac_policy` |
| `friendly_name: "${friendly} ${mac}"` | the device's friendly name |
| `${mac}` elsewhere, undeclared | substituted inline |

Everything else -- comments, blank lines, key order, quoting style, `!secret`
and `!lambda` tags, anchors -- is copied through byte for byte. The generator
locates values with a YAML parser but edits the original text, so nothing is
reformatted on the way through.

A short provenance header is prepended, naming the template, device and MAC. It
carries no timestamp, deliberately: generation is idempotent, so regenerating an
unchanged device produces an identical file.

## MAC addresses

MACs come from Home Assistant's device registry, where the ESPHome integration
records each device with `connections={("mac", …)}`. If Home Assistant has no
MAC for a device, `${mac}` placeholders are **removed** rather than left in
place (a literal `${mac}` would fail to compile), and the panel shows a warning.

`mac_policy` controls what replaces `${mac}` outside the node name:

- `suffix3` (default) -- last three bytes, `aabbcc`, matching ESPHome's own
  `name_add_mac_suffix` format
- `full` -- `aabbccddeeff`
- `strip` -- remove the placeholder and any separator right before it

## Options

| Option | Default | Notes |
|---|---|---|
| `esphome_config_dir` | `/homeassistant/esphome` | Where ESPHome YAML lives |
| `templates_dir` | `/config/templates` | Your base templates |
| `scan_interval_minutes` | `15` | 1–1440 |
| `auto_generate` | `true` | Off = report only |
| `scan_on_startup` | `true` | |
| `mac_policy` | `suffix3` | `suffix3` / `full` / `strip` |
| `name_add_mac_suffix_action` | `set_false` | `set_false` / `remove` |
| `dry_run` | `false` | Report without writing |
| `log_level` | `info` | |

## Troubleshooting

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

**A device shows "No template".**
No template matched its node name. Either name a template after the device's
prefix, or add `# x-match-prefix:` to an existing one. The panel's **Templates**
panel lists every rule currently in force.

**The node name looks wrong.**
The ESPHome node name is not directly exposed over Home Assistant's WebSocket
API, so it is derived: config entry title first (which ESPHome sets from the
device name), then a slugified friendly name, then `esphome-<mac>`. The
`name_source` field on each row records which was used. If it guessed wrong,
create the YAML by hand -- the add-on will then skip that device.

**Nothing is being written.**
Check `dry_run` and `auto_generate`, and that `esphome_config_dir` points at a
directory the add-on can write to.
