# Satel INTEGRA Plus

A custom [Home Assistant](https://www.home-assistant.io/) integration for
**Satel INTEGRA** alarm / home-automation panels connected through an
**ETHM-1 / ETHM-1 Plus** Ethernet module.

Unlike the stock `satel_integra` integration, this one treats the panel as
the source of truth: it **discovers your whole installation from the panel
itself** — every partition, zone and output with its name and function — and
creates ready-to-use entities for alarms, sensors, roller shutters, gates,
lights and floor heating. No YAML, no zone lists.

## Why not the built-in `satel_integra`?

| | built-in `satel_integra` | `satel_integra_ext` fork | **this integration** |
|---|---|---|---|
| Configuration | YAML zone/output lists | YAML | **UI config flow + auto-discovery** |
| Entity names | typed by hand | typed by hand | **read from the panel** |
| Temperature sensors | ✗ | ✓ (unstable) | ✓ |
| Roller shutter covers | ✗ (two switches) | ✗ | ✓ **native covers + group outputs** |
| Cover groups move all covers | ✗ | partially | ✓ (one wire frame per action) |
| Gates / garage doors | switch only | switch only | ✓ covers with open/closed state |
| Floor heating (thermostat outputs) | ✗ | ✗ | ✓ climate entities |
| Connection stability | fire-and-forget writes | frequent disconnects | serialized queue, keepalive, auto-reconnect |
| 256-object panels (INTEGRA 256 Plus) | partial | partial | ✓ extended protocol autodetected |

The connection layer was rebuilt from the protocol specification: exactly one
command in flight at a time, every response correlated to its request, output
commands batched into single bitmask frames, idle keepalive under the
module's 25-second cutoff, and reconnection with backoff. The known failure
modes of the older integrations (a slow temperature reply desynchronizing
the stream, concurrent commands being dropped by the module) are prevented
structurally, and the test suite simulates a full panel to prove it.

## What you get

After setup the integration creates, automatically:

- **`alarm_control_panel`** — one per partition. Arm away / home / night
  (each mapped to a configurable panel arm mode 0–3), disarm, alarm
  clearing. If the panel refuses to arm because zones are violated, the
  integration can automatically retry as *force-arm* (on by default,
  like answering "arm anyway?" on a keypad).
- **`binary_sensor`** — one per zone. The device class is inferred from the
  zone's reaction type and name: motion, glass break, water leak, smoke,
  gas, door/garage, panic, low temperature. Tamper/alarm/bypass states are
  exposed as attributes.
- **`sensor`** — room temperature for every zone with a temperature-capable
  detector (ABAX ATD). Read via the protocol's `0x7D` command in a slow
  round-robin (wireless detectors refresh on a minutes-scale cycle anyway).
- **`cover`** (shutter) — for every roller-blind output pair (panel output
  functions 105 "up" / 106 "down"). Open / close / stop; shows *opening* /
  *closing* while an output runs and remembers the last completed direction.
  Installer whole-group outputs (commonly named `ROL …`) become covers too —
  one tap moves a whole floor using the panel's own grouping.
  **HA cover groups work correctly**: simultaneous commands from a group are
  merged into a single panel command, so every member moves.
- **`cover`** (gate/garage) — for momentary (MONO) outputs that drive gates.
  Open/closed state comes from the gate's reed-contact zone, matched by name
  or configured explicitly.
- **`light`** — bistable outputs named as lighting (`Ośw…`).
- **`switch`** — every other controllable output (valves, mode flags,
  momentary pulses).
- **`climate`** — heat-only thermostats for panel-native *thermostat*
  outputs (function 120, INTEGRA firmware 1.19+). Shows the room
  temperature and heating activity; a shared comfort/eco preset is available
  if your installer configured a threshold-switching output. Setpoints are
  panel configuration (DLOADX) — the public integration protocol simply has
  no command to change them, so no integration can.

State updates are push-style: the integration polls the cheap "what changed"
command (`0x7F`) once a second and re-reads only changed state blocks, so a
motion event appears in HA in about a second.

## Requirements

- Satel INTEGRA panel: 24 / 32 / 64 / 128 / 128-WRL / 64 Plus / 128 Plus /
  256 Plus (extended 32-byte protocol used automatically where supported)
- ETHM-1 (fw ≥ 1.06) or ETHM-1 Plus module with the **integration protocol
  enabled**: DLOADX → *Structure → Hardware → keypads → ETHM-1* → enable
  *Integration*; leave *integration encryption* **off** (not yet supported)
- A dedicated panel **user** for Home Assistant. Its code authorizes every
  control command. Two things to check in the user's settings:
  - it has **access to the partitions** you want to arm/disarm — the panel
    *silently ignores* commands for partitions the user cannot access
    (the integration logs the user's access list at startup to help spot this)
  - outputs you want to switch are controllable for that user

> **One client only:** the ETHM integration port serves a single TCP client.
> Disable/remove any other Satel integration before setup, otherwise the
> panel answers `Busy!` and setup fails.

## Installation

**HACS** (recommended): HACS → Integrations → ⋮ → *Custom repositories* →
add `boruc5004/better_satel_integra-ha` as type *Integration* → install →
restart Home Assistant.

**Manual**: copy `custom_components/satel_integra_plus/` into your HA
`config/custom_components/` directory and restart.

Then *Settings → Devices & Services → Add Integration → **Satel INTEGRA
Plus*** and fill in:

| Field | Meaning |
|---|---|
| Host | IP of the ETHM-1 module |
| Port | integration port, default `7094` |
| Panel user code | the dedicated HA user's code (digits) |
| Code prefix | only if your panel uses code prefixes; usually empty |

The first setup enumerates the panel — expect about a minute. The result is
cached, so later restarts connect instantly. After changing the panel
configuration in DLOADX, call the **`satel_integra_plus.rediscover`**
service to refresh the entity list.

## Options

*Settings → Devices & Services → Satel INTEGRA Plus → Configure*:

| Option | Default | Meaning |
|---|---|---|
| Arm home mode | `3` | which panel arm mode (1–3) HA's *Arm home* uses |
| Arm night mode | `2` | which panel arm mode HA's *Arm night* uses |
| Force-arm automatically | on | retry as force-arm when the panel refuses due to violated zones |
| Comfort/eco output | `0` (off) | output number that switches thermostat thresholds, if you have one |
| Skip zone patterns | `ASW-*` | comma-separated name patterns of zones to hide (wall-button inputs, logic zones) |
| Gate state zones | `{}` | JSON: gate output → reed-contact zone |
| Climate bindings | `{}` | JSON: thermostat output → temperature zone |

### The two JSON maps, explained

The integration protocol exposes names and functions, but **not** two
relations that exist only in the panel's internal wiring:

1. **which zone tells a gate's open/closed state** (the reed contact), and
2. **which temperature zone a thermostat output regulates**.

The integration first tries to resolve both **by name**: a gate output named
`Brama wjazdowa` binds to a zone named `Brama wjazdowa`; a thermostat output
named `Kuchnia` binds to a temperature zone named `Kuchnia` (case,
punctuation and diacritics are ignored when matching). If your installer
named things consistently, you don't need the JSON maps at all.

For pairs the name matching can't find, fill the maps with object numbers:

```json
{"25": 57, "26": 25}
```

means *gate on output 25 → state zone 57* and *gate on output 26 → state
zone 25*. Same shape for climate bindings — thermostat output number →
temperature zone number:

```json
{"145": 203, "146": 203}
```

**Where do the numbers come from?**

- Every entity exposes its numbers as attributes — open any gate/climate
  entity in HA and check `output`, `state_zone` / `thermostat_output`,
  `temperature_zone` (unbound ones show `null`).
- Your installer's DLOADX printouts (zones/outputs lists) show the same
  numbers; `scripts/parse_map.py` can turn those printout PDFs into JSON.
- `python3 scripts/live_check.py <panel-ip> --full-discovery` lists every
  output with its function and name, read-only, straight from the panel.

## Services

| Service | Effect |
|---|---|
| `satel_integra_plus.rediscover` | drop the cached device list and re-enumerate the panel (~1 min) |

## Troubleshooting

- **"The integration port is busy"** — something else is connected: the old
  Satel integration, another HA instance, or an integration bridge. Only one
  client can use the port.
- **Arming does nothing, no error** — the panel accepted the command but the
  HA user has no access to that partition. Check the startup log line
  `panel user #N … has access to partitions […]` and fix the user's
  partition assignment in DLOADX or on a keypad.
- **A gate/climate entity shows no state / no temperature** — the binding
  wasn't found by name; add it to the JSON maps (see above).
- **Entities missing after panel reconfiguration** — run
  `satel_integra_plus.rediscover`.
- **Frequent reconnects** — check the network path to the module; the
  integration logs every disconnect reason. The module also drops the
  connection if anything else briefly claims the integration port.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install pytest pytest-asyncio
.venv/bin/python -m pytest          # full suite incl. a simulated panel
python3 scripts/live_check.py <panel-ip>   # read-only checks against real hardware
```

`custom_components/satel_integra_plus/pysatel/` is a self-contained asyncio
protocol library (no HA imports) — reusable for other Satel projects. See
[docs/DESIGN.md](docs/DESIGN.md) for the architecture and protocol notes.

Based on Satel's public *"INT-RS / ETHM-1 integration protocol"* document
(rev. 2015-03-19). This project is not affiliated with Satel sp. z o.o.

## License

[MIT](LICENSE)
