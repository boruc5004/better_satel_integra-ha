#!/usr/bin/env python3
"""Read-only live check of the pysatel client against a real panel.

Usage: python3 scripts/live_check.py <host> [port] [--full-discovery]

Sends ONLY read commands (version, states, names, temperatures). The
integration port must be free — stop any other integration client first
(the panel serves a single client and answers "Busy!" otherwise).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components" / "satel_integra_plus"))

from pysatel.client import PanelBusyError, ResponseTimeoutError, SatelClient
from pysatel.const import Cmd, DeviceType
from pysatel.frames import bitmask_to_numbers


def usage() -> None:
    print(__doc__)
    sys.exit(1)


async def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        usage()
    host = args[0]
    port = int(args[1]) if len(args) > 1 else 7094
    full = "--full-discovery" in sys.argv

    client = SatelClient(host, port)
    try:
        await client.connect()
    except PanelBusyError:
        print("PORT BUSY: another integration client is connected. "
              "Stop it and retry.")
        return 2
    print(f"panel: {client.panel.model}, fw {client.panel.version}, "
          f"module {client.panel.module_version}, "
          f"32-byte masks: {client.panel.supports_32_bytes}")

    violated = bitmask_to_numbers(await client.state_command(Cmd.ZONES_VIOLATION))
    outputs = bitmask_to_numbers(await client.state_command(Cmd.OUTPUTS_STATE))
    armed = bitmask_to_numbers(await client.state_command(Cmd.PARTITIONS_ARMED))
    print(f"violated zones: {sorted(violated)}")
    print(f"active outputs: {sorted(outputs)}")
    print(f"armed partitions: {sorted(armed)}")

    print("\n--- device name reads (0xEE) ---")
    for devtype, num in [
        (DeviceType.PARTITION, 1),
        (DeviceType.ZONE, 1),
        (DeviceType.OUTPUT, 1),
    ]:
        try:
            func, name = await client.read_device_info(devtype, num)
            print(f"  {devtype.name} #{num}: func={func:3d} name={name!r}")
        except (ResponseTimeoutError, Exception) as err:  # noqa: BLE001
            print(f"  {devtype.name} #{num}: {err}")

    print("\n--- temperature capability scan (zones flagged reaction 56) ---")
    found_temp = False
    max_objects = 256 if client.panel.supports_32_bytes else 128
    for zone in range(1, max_objects + 1):
        try:
            reaction, name = await client.read_device_info(DeviceType.ZONE, zone)
        except Exception:  # noqa: BLE001 - nonexistent zones refuse the read
            continue
        if reaction != 56:
            continue
        found_temp = True
        try:
            temp = await client.read_temperature(zone)
        except ResponseTimeoutError:
            print(f"  zone {zone:3d} {name!r}: no answer")
            continue
        print(f"  zone {zone:3d} {name!r}: "
              f"{temp if temp is not None else 'undetermined'} °C")
    if not found_temp:
        print("  (no low-temperature-reaction zones found)")

    if full:
        print("\n--- full output discovery (nonzero functions) ---")
        for n in range(1, max_objects + 1):
            try:
                func, name = await client.read_device_info(DeviceType.OUTPUT, n)
            except Exception:  # noqa: BLE001
                continue
            if func != 0:
                print(f"  out {n:3d}: func={func:3d} {name!r}")

    await client.close()
    print("\ndone")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
