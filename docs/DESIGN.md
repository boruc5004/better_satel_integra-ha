# Architecture

## Why another Satel integration

- **Official `satel_integra`**: alarm panel + zone binary sensors + plain
  output switches only. No temperature sensors, no covers, no climate.
- **Community forks** add temperature sensors but gate their whole send
  pipeline on a single shared, un-correlated completion event; one slow
  `0x7D` temperature reply desynchronizes the request/response stream and the
  panel's ~25 s idle cutoff eventually drops the link.
- **Cover groups**: modelling a cover as two HA switches (or template covers)
  means a HA cover-group action fans out to N concurrent service calls; with
  a fire-and-forget client the ETHM module executes the first frame and drops
  the rest. The integration protocol natively supports **multi-output
  bitmasks in a single command** — a group action must become ONE frame.

## Layout

```
custom_components/satel_integra_plus/
├── pysatel/                  # self-contained protocol library (no HA imports)
│   ├── frames.py             # framing, CRC, bitmasks (pure functions)
│   ├── const.py              # command codes, result codes, panel types
│   ├── client.py             # asyncio connection + command queue + batching
│   └── monitor.py            # reconnecting hub, discovery, state cache
├── __init__.py               # entry setup, discovery cache (Store)
├── config_flow.py            # host/port/code + options
├── mapping.py                # discovery -> entity descriptors (pure)
├── entity.py                 # push-updated base entity
└── <platforms>.py            # alarm_control_panel, binary_sensor, sensor,
                              # cover, light, switch, climate
```

## Connection layer (`client.py`) — the reliability core

Single TCP connection, one reader task; everything else talks to the panel
through a serialized command path:

- `command()` sends one frame and awaits the matching answer. Exactly ONE
  command is in flight at any time (the spec requires it). Responses match by
  echoed command byte plus payload correlation (`0x7D` echoes the zone,
  `0xEE` echoes device type+number), so a late reply can never satisfy the
  wrong request. `0xEF` results complete the pending command when it is a
  control command, or when they carry an error code (the panel refuses reads
  of nonexistent devices with `0xEF 0x08`); a *success* `0xEF` can never
  complete a state read.
- **Batching**: output-control requests (`0x88` on / `0x89` off) accept a set
  of outputs. A ~50 ms coalescing window merges same-action requests into ONE
  32-byte-bitmask frame — an HA cover-group "close N covers" becomes a single
  wire command. ON and OFF never merge.
- **Keepalive/watchdog**: the module drops idle connections after ~25 s; the
  client sends a cheap `0x1A` read when outbound-idle. A keepalive timeout is
  treated as a dead (half-open) link and force-drops the connection so the
  hub reconnects.
- The `Busy!` ASCII banner at connect (another client owns the single
  integration slot) surfaces as a distinct error with its own retry cadence.

## Monitoring (`monitor.py`)

- Poll `0x7F` *list of new data* at 1 Hz: one cheap query says which state
  blocks changed; only those are re-read (extended 32-byte variants on
  256-object panels). A periodic full resync covers bits the module cannot
  report. State lands in a cache; changes push to entity listeners.
- Temperatures rotate slowly (one `0x7D` per 15 s; ABAX wireless detectors
  refresh on a minutes-scale cycle anyway; replies may take up to 5 s). Zones
  that never answer are dropped from the rotation.
- Reconnect supervisor with exponential backoff; discovery aborts (and is
  never cached) if the connection drops mid-enumeration.

## Discovery (satel-first)

`0xEE` name reads for partitions, zones (device type 5 also returns the
partition assignment) and outputs return the name plus a type/function byte:
for outputs the DLOADX function (105/106 roller up/down, 24 MONO, 25 BI,
120 thermostat), for zones the reaction type. Mapping rules (`mapping.py`):

- consecutive 105+106 output pairs → `cover` (installer group outputs
  detected by the common `ROL ` naming convention are covers too)
- MONO outputs named as gates → `cover` (gate/garage) with a reed-contact
  state zone bound by identical normalized name, overridable via options
- BI outputs named as lighting → `light`, other controllable outputs → `switch`
- function-120 outputs → `climate`, bound to a temperature zone by name or
  options; heat-only (the protocol exposes no thermostat setpoints)
- zones → `binary_sensor` (device class from reaction type, then name
  heuristics: motion/glass/water/smoke/gas/door); reaction-56 zones
  (low temperature) additionally become temperature `sensor`s
- partitions → `alarm_control_panel` (arm modes 0–3, optional automatic
  force-arm on refusal `0x11`, disarm + clear alarm)

Names are decoded as CP1250 (Polish firmware default). Discovery results are
cached in HA storage; the `rediscover` service refreshes them.

## Notable protocol facts (rev. 2015-03-19)

- Frame `FE FE | cmd data… | crc16 | FE 0D`, `FE` escaped as `FE F0`; CRC is
  the documented rotate-xor-add over cmd+data.
- 256-object panels: append one byte to state reads for 32-byte answers;
  detected via `0x7E` (INTEGRA type) + `0x7C` (module capability bit).
- Control commands carry an 8-byte nibble-packed user code; the panel answers
  "accepted" regardless of the user's partition rights and silently ignores
  commands for partitions the user cannot access — the hub logs the user's
  own rights record (`0xE0`) at connect to make this visible.
- Only one integration client per module; ~25 s idle timeout.
